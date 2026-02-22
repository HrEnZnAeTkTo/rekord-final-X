import time
import threading
import traceback
from pywinauto import Desktop
from pythonosc import dispatcher, osc_server, udp_client

# === КОНФИГУРАЦИЯ ===
RUST_PORT = 4455       # Порт, куда Rust шлет инфу (дека + время)
NODE_PORT = 4460       # Порт RekordKaraoke (Node.js)
POLL_INTERVAL = 0.5    # Как часто проверять экран (сек)

# Глобальные переменные
current_master_deck = 0 
last_sent_track_info = ""

# --- ПЕРЕМЕННЫЕ ДЛЯ СИНХРОНИЗАЦИИ ---
last_time_update = 0        # Время получения последнего пакета от Rust
is_playing = False          # Текущее состояние воспроизведения

# Клиент для отправки в Node.js
sender = udp_client.SimpleUDPClient("127.0.0.1", NODE_PORT)

# === ЧТЕНИЕ ЭКРАНА ===
class UIReader:
    def __init__(self):
        self.app_title = ".*rekordbox.*"
        self.anchor = "4Deck Horizontal"
        self.d1_indices = (133, 135) 
        self.d2_indices = (156, 158)
        self.window = None
        self.container = None

    def connect(self):
        try:
            desktop = Desktop(backend="uia")
            windows = desktop.windows(title_re=self.app_title)
            self.window = next((w for w in windows), None)
            
            if self.window:
                if self.window.is_minimized():
                    print("[UI] Window is minimized. Restoring to find elements...")
                    self.window.restore()
                    time.sleep(0.5)

                for el in self.window.descendants():
                    if el.window_text() == self.anchor:
                        self.container = el.parent()
                        print("[UI] Connected to Rekordbox Window")
                        return True
            return False
        except Exception as e:
            print(f"[Connection Error] {e}")
            return False

    def check_window_state(self):
        if not self.window:
            return False
        try:
            if self.window.is_minimized():
                print("[UI] Detect minimized window -> Restoring...")
                self.window.restore()
                time.sleep(0.2)
            return True
        except Exception:
            self.window = None
            self.container = None
            return False

    def get_track_info(self, deck_index):
        if not self.check_window_state():
            if not self.connect(): return None
        
        if not self.container:
            if not self.connect(): return None
        
        try:
            children = self.container.children()
            
            if len(children) < 160: 
                print("[UI] Layout changed or incomplete. Reconnecting...")
                self.connect()
                return None
            
            indices = self.d1_indices if deck_index == 0 else self.d2_indices
            
            # ЗАЩИТА ОТ ВЫЛЕТА ПО ИНДЕКСУ
            if indices[0] >= len(children) or indices[1] >= len(children):
                print(f"[UI Error] Indices {indices} out of range (len={len(children)})")
                return None

            track = children[indices[0]].window_text()
            artist = children[indices[1]].window_text()
            
            return artist, track
        except Exception as e:
            print(f"[Read Error] {e}") 
            self.container = None
            return None

reader = UIReader()

# === ПОТОК 1: СЛУШАЕМ RUST ===

def handle_time(address, *args):
    global last_time_update
    last_time_update = time.time()

def handle_deck_change(address, *args):
    global current_master_deck, last_sent_track_info
    try:
        new_deck = int(args[0])
        if new_deck in [0, 1]:
            if current_master_deck != new_deck:
                print(f"[OSC] Rust switched master to Deck {new_deck + 1}")
                current_master_deck = new_deck
                
                # При смене деки шлем пустые строки
                sender.send_message("/track/master/title", "")
                sender.send_message("/track/master/artist", "")
                last_sent_track_info = ""
                
    except Exception:
        traceback.print_exc()

def start_osc_listener():
    try:
        dp = dispatcher.Dispatcher()
        dp.map("/deck/master", handle_deck_change)
        dp.map("/time/master", handle_time)
        server = osc_server.ThreadingOSCUDPServer(("0.0.0.0", RUST_PORT), dp)
        print(f"[Proxy] Listening for Deck Info & Time on {RUST_PORT}...")
        server.serve_forever()
    except Exception as e:
        print(f"[OSC Listener Fatal Error] {e}")
        traceback.print_exc()

# === ПОТОК 2: ПРОВЕРЯЕМ ЭКРАН (POLLING) ===
def start_polling():
    global last_sent_track_info, is_playing
    print(f"[Poller] Started checking UI every {POLL_INTERVAL}s...")
    
    while True:
        try:
            # 1. PLAY/PAUSE WATCHDOG
            now = time.time()
            new_state = (now - last_time_update) < 0.3

            if new_state != is_playing:
                is_playing = new_state
                status_str = "playing" if is_playing else "paused"
                print(f"[State] {status_str.upper()}")
                sender.send_message("/status/playing", 1 if is_playing else 0)

            # 2. ЧТЕНИЕ ТРЕКА
            res = reader.get_track_info(current_master_deck)
            
            if res:
                artist, title = res
                
                # Если названия нет - пропускаем
                if not title or title == "Loading...":
                    time.sleep(POLL_INTERVAL)
                    continue

                # ИСПРАВЛЕНИЕ: Если артиста нет, делаем пустую строку
                if not artist or artist == "Loading...":
                    artist = ""
                
                if artist:
                    combined = f"{artist} - {title}"
                else:
                    combined = title 

                if combined != last_sent_track_info:
                    print(f">>> DETECTED NEW TRACK: {combined}")
                    
                    sender.send_message("/track/master/artist", artist)
                    sender.send_message("/track/master/title", title)
                    
                    last_sent_track_info = combined
            
        except Exception:
            print("[Poller Error]")
            traceback.print_exc()
            time.sleep(1)
            
        time.sleep(POLL_INTERVAL)

# === MAIN ===
if __name__ == "__main__":
    try:
        t = threading.Thread(target=start_osc_listener, daemon=True)
        t.start()
        start_polling()
    except KeyboardInterrupt:
        pass
    except Exception:
        print("CRITICAL ERROR IN MAIN:")
        traceback.print_exc()
        input("Press Enter to exit...")