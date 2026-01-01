import flet as ft
import os  # [修复] 确保导入 os
import time
import threading
import queue
import requests
import yt_dlp
import google.generativeai as genai
import glob
import re
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================= 视觉常量 (White & Ink Green Style) =================
THEME_BG = "#FFFFFF"          # 纯白背景
THEME_ACCENT = "#006400"      # 墨绿色 (Ink Green)
THEME_TEXT = "#006400"        # 墨绿文字
THEME_LIGHT_GRAY = "#F0F0F0"  # 极浅灰 (用于输入框背景)

# 字体配置 (移动端优先使用系统默认)
FONT_UI = "Roboto"
FONT_CODE = "Roboto Mono"

# ================= 1. 核心业务逻辑 (Backend) =================
class MatrixBackend:
    def __init__(self, msg_queue):
        self.queue = msg_queue
        self.api_key = ""
        self.proxy_url = ""
        self.proxies = {}
        self.model_name = "gemini-2.5-flash"

    def log(self, message):
        self.queue.put(("log", message))

    def update_status(self, status):
        self.queue.put(("status", status))

    def setup_config(self, api_key, proxy_port):
        self.api_key = api_key
        port = proxy_port if proxy_port else "7890"
        self.proxy_url = f"http://127.0.0.1:{port}"
        self.proxies = {'http': self.proxy_url, 'https': self.proxy_url}
        os.environ['HTTP_PROXY'] = self.proxy_url
        os.environ['HTTPS_PROXY'] = self.proxy_url
        genai.configure(api_key=self.api_key)

    def get_retry_session(self, retries=3):
        session = requests.Session()
        retry = Retry(total=retries, read=retries, connect=retries, backoff_factor=1, status_forcelist=(500, 502, 504))
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        session.proxies.update(self.proxies)
        return session

    def sanitize_filename(self, name):
        if not name: return "unknown_payload"
        return re.sub(r'[\\/*?:"<>|]', "", name).strip()

    def get_save_path(self, filename):
        """[移动端适配] 智能路径选择"""
        # 尝试安卓标准下载目录
        android_download = "/storage/emulated/0/Download"
        if os.path.exists(android_download):
            return os.path.join(android_download, filename)
        # 回退到当前目录 (PC调试用)
        return filename

    def clean_vtt_tags(self, text):
        if not text: return ""
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'align:start position:[\d%]+', '', text)
        lines = []
        seen = set()
        for line in text.splitlines():
            line = line.strip()
            if not line or 'WEBVTT' in line or '-->' in line: continue
            if line.isdigit(): continue
            if line not in seen:
                lines.append(line)
                seen.add(line)
        return " ".join(lines)

    def upload_file_manual(self, file_path, mime_type):
        file_size = os.path.getsize(file_path)
        self.log(f"-> UPLOAD: {file_size / 1024 / 1024:.2f} MB")
        
        upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={self.api_key}&uploadType=media"
        headers = {
            'X-Goog-Upload-Protocol': 'raw', 
            'X-Goog-Upload-Command': 'start, upload, finalize', 
            'Content-Type': mime_type, 
            'Content-Length': str(file_size)
        }
        session = self.get_retry_session()

        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                with open(file_path, 'rb') as f:
                    response = session.post(upload_url, headers=headers, data=f, timeout=(30, 1200))
                response.raise_for_status()
                data = response.json()
                file_uri = data['file']['uri']
                file_name = data['file']['name']
                
                self.log("   >> VERIFYING CLOUD ASSET...")
                check_url = f"https://generativelanguage.googleapis.com/v1beta/{file_name}?key={self.api_key}"
                while True:
                    state = session.get(check_url, timeout=30).json().get('state')
                    if state == "ACTIVE": return file_uri
                    if state == "FAILED": raise ValueError("Processing Failed")
                    time.sleep(2)
            except Exception as e:
                self.log(f"   [ERROR] UPLOAD: {e}")
                if attempt < max_attempts: time.sleep(5)
                else: raise e

    def run_process(self, video_url):
        target_audio_file = None
        try:
            self.update_status("STATUS: INITIALIZING...")
            
            # [关键] 无 FFmpeg 依赖配置
            common_opts = {
                'quiet': True, 'no_warnings': True, 'nocolor': True,
                'force_ipv4': True, 'nocheckcertificate': True,
                'socket_timeout': 60, 'retries': 20, 'fragment_retries': 20, 'ignoreerrors': True,
                'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
            }

            self.log("-> [SCAN] ANALYZING URL...")
            try:
                with yt_dlp.YoutubeDL({**common_opts, 'skip_download': True}) as ydl:
                    info = ydl.extract_info(video_url, download=False)
                    title = info.get('title', 'Unknown_Target')
                    duration = info.get('duration', 0)
            except Exception as e:
                self.log(f"[FATAL] SCAN ERROR: {e}")
                self.queue.put(("finish", None))
                return

            self.log(f"-> TITLE: {title[:20]}...")
            
            # 策略选择
            subs = info.get('subtitles', {})
            auto_subs = info.get('automatic_captions', {})
            target_lang, is_auto = None, False
            if any(k.startswith('zh') for k in subs): target_lang, is_auto = next(k for k in subs if k.startswith('zh')), False
            elif any(k.startswith('en') for k in subs): target_lang, is_auto = next(k for k in subs if k.startswith('en')), False
            elif auto_subs: target_lang, is_auto = list(auto_subs.keys())[0], True

            downloaded_text = None
            if target_lang:
                try:
                    self.update_status("STATUS: FETCHING SUBS...")
                    ydl_opts_sub = {
                        **common_opts, 'skip_download': True, 'outtmpl': 'temp_subs',
                        'subtitleslangs': [target_lang], 'writeautomaticsub': is_auto, 'writesubtitles': not is_auto
                    }
                    with yt_dlp.YoutubeDL(ydl_opts_sub) as ydl: ydl.download([video_url])
                    files = glob.glob("temp_subs.*.vtt")
                    if files:
                        with open(files[0], 'r', encoding='utf-8') as f: raw = f.read()
                        os.remove(files[0])
                        downloaded_text = self.clean_vtt_tags(raw)
                        self.log(">> SUBTITLES FOUND.")
                except Exception: pass

            model = genai.GenerativeModel(self.model_name)
            result_text = ""

            if downloaded_text and len(downloaded_text) > 50:
                self.update_status("STATUS: AI PROCESSING...")
                prompt = f"请整理以下视频字幕，生成详细的中英对照内容，并写一份结构化的中文总结：\n\n{downloaded_text}"
                result_text = model.generate_content(prompt).text
            else:
                self.update_status("STATUS: DOWNLOADING AUDIO...")
                # 清理
                for f in glob.glob("temp_audio.*"):
                    try: os.remove(f)
                    except: pass
                
                # [关键] 纯音频下载配置
                ydl_opts_audio = {
                    **common_opts,
                    'format': 'bestaudio/best',      
                    'outtmpl': 'temp_audio.%(ext)s', 
                    'prefer_ffmpeg': False,          
                    'postprocessors': [],            
                }
                
                try:
                    with yt_dlp.YoutubeDL(ydl_opts_audio) as ydl: ydl.download([video_url])
                    found_files = glob.glob("temp_audio.*")
                    valid_files = [f for f in found_files if not f.endswith(('.part', '.ytdl', '.vtt'))]
                    
                    if not valid_files: raise Exception("File not found")
                    target_audio_file = valid_files[0]
                    mime_type = "audio/webm" if target_audio_file.endswith(".webm") else "audio/mp4"
                    
                except Exception as e:
                    self.log(f"[FATAL] AUDIO FAIL: {e}")
                    self.queue.put(("finish", None))
                    return

                self.update_status("STATUS: UPLOADING...")
                file_uri = self.upload_file_manual(target_audio_file, mime_type)
                
                self.update_status("STATUS: AI LISTENING...")
                STEP = 3600
                current = 0
                full_parts = []
                while current < duration:
                    end = min(current + STEP, duration)
                    self.log(f"-> SECTOR: {current}s - {end}s")
                    prompt = f"请听写并翻译这段音频从 {current}秒 到 {end}秒 的内容。遵守“一句原文、一句中文翻译”的交替格式。"
                    try:
                        resp = model.generate_content(
                            [{"file_data": {"mime_type": mime_type, "file_uri": file_uri}}, prompt], 
                            request_options={"timeout": 600}
                        )
                        full_parts.append(resp.text)
                    except Exception as e:
                        self.log(f"   [ERR] SECTOR FAIL: {e}")
                    current += STEP
                
                result_text = "\n".join(full_parts)
                if target_audio_file and os.path.exists(target_audio_file): os.remove(target_audio_file)

            self.log("-> FINALIZING...")
            summary = model.generate_content(f"请对以下记录进行结构化中文总结：\n{result_text}").text
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_title = self.sanitize_filename(title)[:30]
            filename = f"DECODED_{safe_title}_{timestamp}.txt"
            save_path = self.get_save_path(filename)
            
            final_output = f"TARGET: {title}\nTIMESTAMP: {timestamp}\nSOURCE: {video_url}\n{'-'*30}\n\n{result_text}\n\n=== SUMMARY ===\n{summary}"
            
            try:
                with open(save_path, "w", encoding="utf-8") as f: f.write(final_output)
                self.log(f"-> SAVED: {save_path}")
            except Exception as e:
                self.log(f"[WARN] SAVE FAILED: {e}")
                self.log(">> CHECK STORAGE PERMISSIONS")

            self.update_status("STATUS: DONE")
            self.queue.put(("finish", final_output))

        except Exception as e:
            self.log(f"[CRITICAL] {e}")
            self.update_status("STATUS: ERROR")
            self.queue.put(("finish", None))
            if target_audio_file and os.path.exists(target_audio_file):
                try: os.remove(target_audio_file)
                except: pass

# ================= 2. UI 组件 (Flet Mobile Style) =================

def create_card(controls_list):
    """墨绿风格卡片容器"""
    return ft.Container(
        content=ft.Column(controls_list, spacing=10),
        bgcolor=THEME_BG,
        border=ft.border.all(1.5, THEME_ACCENT), # 实线墨绿边框
        border_radius=10,
        padding=15,
        margin=ft.margin.only(bottom=10)
    )

def create_input(label, hint, password=False):
    """墨绿风格输入框"""
    return ft.TextField(
        label=label,
        hint_text=hint,
        password=password,
        can_reveal_password=True if password else False,
        color=THEME_TEXT,
        border_color=THEME_ACCENT,
        cursor_color=THEME_ACCENT,
        label_style=ft.TextStyle(color=THEME_ACCENT, font_family=FONT_UI),
        text_style=ft.TextStyle(font_family=FONT_CODE, size=14),
        bgcolor=THEME_LIGHT_GRAY,
        border_radius=8,
        content_padding=15,
        text_size=14
    )

# ================= 3. 主程序 (Flet App) =================

def main(page: ft.Page):
    # 1. 页面基础配置 (Light Mode + Mobile Ready)
    page.title = "INK DECODER"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = THEME_BG
    page.padding = 10
    page.scroll = ft.ScrollMode.AUTO # [关键] 开启全局滚动适配手机
    
    # 消息队列
    msg_queue = queue.Queue()
    backend = MatrixBackend(msg_queue)

    # --- UI 构建 ---

    # 标题 (使用 ft.Alignment 替代 alignment.center)
    header = ft.Container(
        content=ft.Text("INK // DECODER", size=24, weight=ft.FontWeight.BOLD, color=THEME_ACCENT, font_family=FONT_UI),
        alignment=ft.Alignment(0, 0), # [修复] 坐标写法 (0,0) 为居中
        padding=ft.padding.only(top=20, bottom=20)
    )

    # 卡片 1: 配置
    inp_api = create_input("API Key", "Google Gemini API Key", password=True)
    inp_proxy = create_input("Proxy Port", "7890 (Optional)")
    inp_url = create_input("YouTube URL", "Video Link")
    inp_proxy.value = "7890"

    card_config = create_card([
        ft.Text("CONFIGURATION", color=THEME_ACCENT, weight=ft.FontWeight.BOLD, size=14, font_family=FONT_UI),
        inp_api,
        inp_proxy,
        inp_url
    ])

    # 卡片 2: 操作
    status_text = ft.Text("STATUS: READY", color=THEME_ACCENT, font_family=FONT_CODE, size=12)
    
    # [修复] 进度条: 使用 value=None 来表示不确定进度，避免 ProgressBarType 报错
    progress_bar = ft.ProgressBar(
        width=None, 
        color=THEME_ACCENT, 
        bgcolor=THEME_LIGHT_GRAY, 
        value=0, # 初始为0
        visible=False
    )
    
    def on_btn_click(e):
        if not inp_api.value or not inp_url.value:
            log_list.controls.append(ft.Text(">> ERROR: Input Required", color="red", font_family=FONT_CODE))
            page.update()
            return

        btn_action.disabled = True
        btn_action.content.value = "PROCESSING..." # 修改 Text 内容
        progress_bar.visible = True
        progress_bar.value = None # [修复] 设置为 None 开启无限加载动画
        page.update()

        # 启动线程
        backend.setup_config(inp_api.value, inp_proxy.value)
        threading.Thread(target=backend.run_process, args=(inp_url.value,), daemon=True).start()

    # [修复] 按钮: 使用 content=ft.Text() 替代 text=...
    btn_action = ft.OutlinedButton(
        content=ft.Text("INITIATE SEQUENCE", color=THEME_ACCENT, weight=ft.FontWeight.BOLD),
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=8),
            side=ft.BorderSide(2, THEME_ACCENT),
            overlay_color="#E0FFE0", # 浅绿悬停
        ),
        on_click=on_btn_click,
        height=50,
    )

    card_action = create_card([
        status_text,
        progress_bar,
        ft.Container(height=5),
        btn_action
    ])

    # 卡片 3: 日志 (ListView 适合移动端性能)
    log_list = ft.ListView(height=200, spacing=2, auto_scroll=True)
    
    # 结果显示 (TextField 只读)
    result_box = ft.TextField(
        multiline=True,
        read_only=True,
        min_lines=3,
        text_size=12,
        color=THEME_TEXT,
        border_color=THEME_ACCENT,
        bgcolor=THEME_LIGHT_GRAY,
        label="DECODED OUTPUT",
        visible=False
    )

    card_terminal = create_card([
        ft.Text("TERMINAL OUTPUT", color=THEME_ACCENT, weight=ft.FontWeight.BOLD, size=14, font_family=FONT_UI),
        ft.Container(
            content=log_list,
            bgcolor=THEME_LIGHT_GRAY,
            border_radius=5,
            padding=5,
            border=ft.border.all(1, "#E0E0E0")
        ),
        result_box
    ])

    # 添加组件到页面 (Column 布局)
    page.add(
        ft.Column([
            header, 
            card_config, 
            card_action, 
            card_terminal
        ])
    )

    # --- 后台 UI 监控线程 ---
    def monitor_queue():
        while True:
            try:
                # 阻塞获取，降低 CPU
                msg = msg_queue.get() 
                msg_type, content = msg
                
                if msg_type == "log":
                    t = datetime.now().strftime("%H:%M:%S")
                    log_list.controls.append(ft.Text(f"[{t}] {content}", color=THEME_TEXT, font_family=FONT_CODE, size=12))
                
                elif msg_type == "status":
                    status_text.value = f"STATUS: {content}"
                
                elif msg_type == "finish":
                    progress_bar.visible = False
                    progress_bar.value = 0
                    btn_action.disabled = False
                    btn_action.content.value = "INITIATE SEQUENCE"
                    
                    if content:
                        result_box.value = content
                        result_box.visible = True
                        log_list.controls.append(ft.Text(">> TASK COMPLETE.", color=THEME_ACCENT, weight=ft.FontWeight.BOLD))
                    else:
                        log_list.controls.append(ft.Text(">> TASK FAILED.", color="red", weight=ft.FontWeight.BOLD))
                
                page.update()
            except Exception as e:
                print(f"UI Error: {e}")

    threading.Thread(target=monitor_queue, daemon=True).start()

if __name__ == "__main__":
    ft.app(target=main)

