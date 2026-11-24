#!/usr/bin/env python3
"""
TransOverlay — 초고속 Google 화면 번역 오버레이 (다국어 지원)

특징:
- Google 번역 전용 + 주요 12개 언어 선택 및 빠른 전환(⇄)
- 절제된 네이티브 다크 UI (Windows 11 / PowerToys 스타일, 깔끔한 설정 구성)
- Windows 언어 팩 미설치 시에도 자동으로 고정밀 클라우드 OCR 폴백
- 전체화면 캡처와 영역 지정 캡처 간 동일한 전처리 스케일로 일관된 번역 결과 보장
- 극한 속도 최적화: PowerShell StringBuilder 고속 출력, Keep-Alive 단일 배치, 번역 캐시, 8-bit BMP, 전체 복사 제거
- 게임 위 최상단 오버레이, 저수준 단축키, 마우스 클릭 즉시 닫기, 외부 분리형 컨트롤 바
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import http.client
import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat
from pynput import keyboard as pynput_keyboard
from pynput import mouse as pynput_mouse
from PyQt6.QtCore import QObject, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QIcon,
    QImage,
    QKeyEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "TransOverlay"
CONFIG_DIR = Path.home() / ".screen_translate_overlay"
CONFIG_PATH = CONFIG_DIR / "config.json"


# ---------------------------------------------------------------------------
# 지원 주요 언어 정의: (표시명, Google 코드, WinRT OCR 태그 목록, OCR.space 코드)
# ---------------------------------------------------------------------------

SUPPORTED_LANGUAGES: list[tuple[str, str, list[str], str]] = [
    ("영어 (English)", "en", ["en-US", "en-GB", "en"], "eng"),
    ("한국어 (Korean)", "ko", ["ko-KR", "ko"], "kor"),
    ("일본어 (日本語)", "ja", ["ja-JP", "ja"], "jpn"),
    ("중국어 간체 (简体中文)", "zh-CN", ["zh-Hans-CN", "zh-Hans", "zh-CN"], "chs"),
    ("중국어 번체 (繁體中文)", "zh-TW", ["zh-Hant-TW", "zh-Hant", "zh-TW"], "cht"),
    ("스페인어 (Español)", "es", ["es-ES", "es-MX", "es"], "spa"),
    ("프랑스어 (Français)", "fr", ["fr-FR", "fr"], "fre"),
    ("독일어 (Deutsch)", "de", ["de-DE", "de"], "ger"),
    ("러시아어 (Русский)", "ru", ["ru-RU", "ru"], "rus"),
    ("베트남어 (Tiếng Việt)", "vi", ["vi-VN", "vi"], "eng"),
    ("태국어 (ไทย)", "th", ["th-TH", "th"], "tha"),
    ("인도네시아어 (Bahasa)", "id", ["id-ID", "id", "en-US"], "eng"),
]

LANG_BY_CODE: dict[str, tuple[str, str, list[str], str]] = {
    item[1]: item for item in SUPPORTED_LANGUAGES
}


# ---------------------------------------------------------------------------
# 리소스 경로 및 아이콘 로드
# ---------------------------------------------------------------------------

def resource_path(filename: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    p = base / filename
    if p.exists():
        return p
    return Path(__file__).resolve().parent / filename


def load_app_icon() -> QIcon:
    for name in ("app_icon.png", "app_icon.ico"):
        p = resource_path(name)
        if p.exists():
            return QIcon(str(p))
    pix = QPixmap(64, 64)
    pix.fill(QColor("#27272A"))
    painter = QPainter(pix)
    painter.setPen(QColor("#FAFAFA"))
    painter.setFont(QFont("Malgun Gothic", 22, QFont.Weight.Bold))
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "A가")
    painter.end()
    return QIcon(pix)


# ---------------------------------------------------------------------------
# Win32 헬퍼
# ---------------------------------------------------------------------------

def is_windows_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            params = ""
        else:
            exe = sys.executable
            params = f'"{os.path.abspath(__file__)}"'
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return int(ret) > 32
    except Exception:
        return False


def apply_game_topmost_window(hwnd: int) -> None:
    if sys.platform != "win32" or not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_TOPMOST = 0x00000008
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        HWND_TOPMOST = -1
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040

        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd,
            GWL_EXSTYLE,
            ex_style | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        )
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 설정 모델 (심플하게 정리)
# ---------------------------------------------------------------------------

@dataclass
class AppConfig:
    hotkey_fullscreen: str = "<ctrl>+<shift>+f"
    hotkey_region: str = "<ctrl>+<shift>+r"
    hotkey_close_overlay: str = "<ctrl>+<shift>+x"
    overlay_opacity: int = 100
    close_on_mouse_click: bool = True
    turbo_mode: bool = True
    source_lang: str = "en"
    target_lang: str = "ko"

    @classmethod
    def load(cls) -> "AppConfig":
        try:
            if CONFIG_PATH.exists():
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                valid_fields = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
                cfg = cls(**valid_fields)
                if cfg.source_lang not in LANG_BY_CODE:
                    cfg.source_lang = "en"
                if cfg.target_lang not in LANG_BY_CODE:
                    cfg.target_lang = "ko"
                cfg.turbo_mode = True
                return cfg
        except Exception:
            pass
        return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# 단축키 포맷 변환, Win32 가상 키코드 매핑, 키캡 녹화 위젯
# ---------------------------------------------------------------------------

QT_SPECIAL_KEYS: dict[int, str] = {
    Qt.Key.Key_F1: "<f1>",
    Qt.Key.Key_F2: "<f2>",
    Qt.Key.Key_F3: "<f3>",
    Qt.Key.Key_F4: "<f4>",
    Qt.Key.Key_F5: "<f5>",
    Qt.Key.Key_F6: "<f6>",
    Qt.Key.Key_F7: "<f7>",
    Qt.Key.Key_F8: "<f8>",
    Qt.Key.Key_F9: "<f9>",
    Qt.Key.Key_F10: "<f10>",
    Qt.Key.Key_F11: "<f11>",
    Qt.Key.Key_F12: "<f12>",
    Qt.Key.Key_Space: "<space>",
    Qt.Key.Key_Tab: "<tab>",
    Qt.Key.Key_Return: "<enter>",
    Qt.Key.Key_Enter: "<enter>",
    Qt.Key.Key_Backspace: "<backspace>",
    Qt.Key.Key_Delete: "<delete>",
    Qt.Key.Key_Insert: "<insert>",
    Qt.Key.Key_Home: "<home>",
    Qt.Key.Key_End: "<end>",
    Qt.Key.Key_PageUp: "<page_up>",
    Qt.Key.Key_PageDown: "<page_down>",
    Qt.Key.Key_Up: "<up>",
    Qt.Key.Key_Down: "<down>",
    Qt.Key.Key_Left: "<left>",
    Qt.Key.Key_Right: "<right>",
    Qt.Key.Key_Print: "<print_screen>",
    Qt.Key.Key_Pause: "<pause>",
}

MODIFIER_ONLY_KEYS = {
    Qt.Key.Key_Control,
    Qt.Key.Key_Shift,
    Qt.Key.Key_Alt,
    Qt.Key.Key_Meta,
    Qt.Key.Key_AltGr,
}

VK_MAP: dict[str, int] = {
    "<ctrl>": 0x11,
    "<shift>": 0x10,
    "<alt>": 0x12,
    "<cmd>": 0x5B,
    "<space>": 0x20,
    "<tab>": 0x09,
    "<enter>": 0x0D,
    "<backspace>": 0x08,
    "<delete>": 0x2E,
    "<insert>": 0x2D,
    "<home>": 0x24,
    "<end>": 0x23,
    "<page_up>": 0x21,
    "<page_down>": 0x22,
    "<up>": 0x26,
    "<down>": 0x28,
    "<left>": 0x25,
    "<right>": 0x27,
    "<print_screen>": 0x2C,
    "<pause>": 0x13,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "\\": 0xDC,
    "`": 0xC0,
}
for _i in range(1, 13):
    VK_MAP[f"<f{_i}>"] = 0x6F + _i
for _ch in "abcdefghijklmnopqrstuvwxyz":
    VK_MAP[_ch] = ord(_ch.upper())
for _d in "0123456789":
    VK_MAP[_d] = ord(_d)


def parse_hotkey_to_vks(pynput_hotkey: str) -> list[int]:
    vks: list[int] = []
    for tok in pynput_hotkey.lower().split("+"):
        t = tok.strip()
        if t in VK_MAP:
            vks.append(VK_MAP[t])
        else:
            return []
    return vks


def format_hotkey_display(pynput_hotkey: str) -> str:
    if not pynput_hotkey:
        return "미지정"
    parts = []
    for token in pynput_hotkey.split("+"):
        clean = token.strip().strip("<>")
        mapping = {
            "ctrl": "Ctrl",
            "shift": "Shift",
            "alt": "Alt",
            "cmd": "Win",
            "space": "Space",
            "enter": "Enter",
            "tab": "Tab",
            "backspace": "Backspace",
            "delete": "Del",
            "insert": "Ins",
            "home": "Home",
            "end": "End",
            "page_up": "PgUp",
            "page_down": "PgDn",
            "print_screen": "PrtSc",
        }
        parts.append(mapping.get(clean.lower(), clean.upper()))
    return " + ".join(parts)


class HotkeyRecorderEdit(QLineEdit):
    hotkey_changed = pyqtSignal(str)
    recording_changed = pyqtSignal(bool)

    def __init__(self, initial_hotkey: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._hotkey = initial_hotkey
        self._recording = False
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(32)
        self._refresh_text()

    @property
    def hotkey(self) -> str:
        return self._hotkey

    def set_hotkey(self, value: str) -> None:
        self._hotkey = value
        self._recording = False
        self._refresh_text()

    def _refresh_text(self) -> None:
        if self._recording:
            self.setText("키 조합을 누르세요 (ESC 취소)")
            self.setStyleSheet(
                "QLineEdit { background-color: #2C2518; border: 1px solid #D97706; "
                "border-radius: 6px; padding: 4px 10px; font-weight: 600; "
                "font-size: 12px; color: #FBBF24; }"
            )
        else:
            self.setText(format_hotkey_display(self._hotkey))
            self.setStyleSheet(
                "QLineEdit { background-color: #18181B; border: 1px solid #3F3F46; "
                "border-radius: 6px; padding: 4px 10px; font-weight: 600; "
                "font-size: 12px; color: #E4E4E7; }"
                "QLineEdit:hover { border-color: #71717A; background-color: #27272A; }"
            )

    def mousePressEvent(self, event) -> None:
        if not self._recording:
            self._recording = True
            self.recording_changed.emit(True)
        self._refresh_text()
        self.setFocus()
        super().mousePressEvent(event)

    def focusOutEvent(self, event) -> None:
        if self._recording:
            self._recording = False
            self._refresh_text()
            self.recording_changed.emit(False)
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._recording:
            super().keyPressEvent(event)
            return

        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._recording = False
            self._refresh_text()
            self.recording_changed.emit(False)
            return

        if key in MODIFIER_ONLY_KEYS:
            return

        modifiers = event.modifiers()
        parts: list[str] = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            parts.append("<ctrl>")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            parts.append("<alt>")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            parts.append("<shift>")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            parts.append("<cmd>")

        key_token = self._qt_key_to_pynput(key)
        if not key_token:
            return

        if not parts and not key_token.startswith("<f"):
            self.setText("Ctrl/Alt/Shift 조합 또는 F1~F12")
            return

        parts.append(key_token)
        new_hotkey = "+".join(parts)
        self._hotkey = new_hotkey
        self._recording = False
        self._refresh_text()
        self.recording_changed.emit(False)
        self.hotkey_changed.emit(new_hotkey)

    @staticmethod
    def _qt_key_to_pynput(key: int) -> Optional[str]:
        if key in QT_SPECIAL_KEYS:
            return QT_SPECIAL_KEYS[key]
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            return chr(key).lower()
        if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            return chr(key)
        punctuation = {
            Qt.Key.Key_Minus: "-",
            Qt.Key.Key_Equal: "=",
            Qt.Key.Key_BracketLeft: "[",
            Qt.Key.Key_BracketRight: "]",
            Qt.Key.Key_Semicolon: ";",
            Qt.Key.Key_Apostrophe: "'",
            Qt.Key.Key_Comma: ",",
            Qt.Key.Key_Period: ".",
            Qt.Key.Key_Slash: "/",
            Qt.Key.Key_Backslash: "\\",
            Qt.Key.Key_QuoteLeft: "`",
        }
        return punctuation.get(key)


# ---------------------------------------------------------------------------
# 게임 호환 글로벌 단축키 매니저
# ---------------------------------------------------------------------------

class GlobalHotkeyManager(QObject):
    triggered_fullscreen = pyqtSignal()
    triggered_region = pyqtSignal()
    triggered_close = pyqtSignal()
    hotkey_error = pyqtSignal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._listener: Optional[pynput_keyboard.GlobalHotKeys] = None
        self._poll_stop = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._vk_bindings: list[tuple[list[int], Callable[[], None]]] = []
        self._paused = False
        self._ignore_until = 0.0

    def set_paused(self, paused: bool) -> None:
        """단축키 녹화 중에는 전역 단축키 발동을 일시정지하고, 해제 직후 눌려있는 키를 무시한다."""
        self._paused = bool(paused)
        if not paused:
            self._ignore_until = time.monotonic() + 0.45

    def register(self, config: AppConfig) -> bool:
        self.stop()
        entries = [
            (config.hotkey_fullscreen, lambda: self.triggered_fullscreen.emit()),
            (config.hotkey_region, lambda: self.triggered_region.emit()),
            (config.hotkey_close_overlay, lambda: self.triggered_close.emit()),
        ]

        if sys.platform == "win32":
            bindings: list[tuple[list[int], Callable[[], None]]] = []
            seen: set[tuple[int, ...]] = set()
            for hk, cb in entries:
                if not hk:
                    continue
                vks = parse_hotkey_to_vks(hk)
                if vks and tuple(sorted(vks)) not in seen:
                    seen.add(tuple(sorted(vks)))
                    bindings.append((vks, cb))
            self._vk_bindings = bindings
            self._poll_stop.clear()
            self._poll_thread = threading.Thread(
                target=self._win32_poll_loop, name="Win32HotkeyPoller", daemon=True
            )
            self._poll_thread.start()
            return True

        mapping: dict[str, Callable[[], None]] = {}
        for hk, cb in entries:
            if hk and hk not in mapping:
                mapping[hk] = cb
        if not mapping:
            return True
        try:
            self._listener = pynput_keyboard.GlobalHotKeys(mapping)
            self._listener.daemon = True
            self._listener.start()
            return True
        except Exception as exc:
            self._listener = None
            self.hotkey_error.emit(f"글로벌 단축키 등록 실패: {exc}")
            return False

    def _win32_poll_loop(self) -> None:
        user32 = ctypes.windll.user32
        # 등록 시점에 이미 눌려 있는 키 조합은 발동하지 않도록 초기 상태를 현재 키 상태로 스냅샷!
        prev_down: list[bool] = [
            all((user32.GetAsyncKeyState(vk) & 0x8000) != 0 for vk in vks)
            for vks, _ in self._vk_bindings
        ]
        last_fire = 0.0

        while not self._poll_stop.is_set():
            now = time.monotonic()
            if self._paused or now < self._ignore_until:
                # 일시정지/유예 중에도 눌림 상태를 계속 갱신해 키를 떼기 전에는 절대 발동 안 되게 함
                for idx, (vks, _) in enumerate(self._vk_bindings):
                    prev_down[idx] = all((user32.GetAsyncKeyState(vk) & 0x8000) != 0 for vk in vks)
                time.sleep(0.025)
                continue

            for idx, (vks, cb) in enumerate(self._vk_bindings):
                all_down = all((user32.GetAsyncKeyState(vk) & 0x8000) != 0 for vk in vks)
                if all_down and not prev_down[idx]:
                    if now - last_fire > 0.35:
                        last_fire = now
                        cb()
                prev_down[idx] = all_down
            time.sleep(0.025)

    def stop(self) -> None:
        self._poll_stop.set()
        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=0.5)
        self._poll_thread = None

        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None


# ---------------------------------------------------------------------------
# 초고속 다국어 Windows WinRT OCR (ConvertTo-Json 제거 -> StringBuilder 직출력 40배 가속)
# + 언어팩 미설치 시 자동 클라우드 OCR 폴백
# ---------------------------------------------------------------------------

POWERSHELL_OCR_SCRIPT = r"""
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    return $netTask.Result
}

[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Media.Ocr,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrResult,Windows.Media.Ocr,ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime] | Out-Null

$engineCache = @{}

function Get-OcrEngineForTags($tagCsv) {
    if ($engineCache.ContainsKey($tagCsv)) { return $engineCache[$tagCsv] }
    $tags = $tagCsv -split ','
    foreach ($t in $tags) {
        $tag = $t.Trim()
        if ([string]::IsNullOrEmpty($tag)) { continue }
        try {
            $lang = [Windows.Globalization.Language]::new($tag)
            if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) {
                $eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
                if ($eng -ne $null) {
                    $engineCache[$tagCsv] = $eng
                    return $eng
                }
            }
        } catch {}
    }
    $engineCache[$tagCsv] = $null
    return $null
}

# 설치된 언어 태그 목록 출력 (예: READY|en-US,ko-KR)
$availTags = @()
foreach ($l in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
    $availTags += $l.LanguageTag
}
Write-Output ("READY|" + ($availTags -join ','))

while ($true) {
    $reqLine = [Console]::In.ReadLine()
    if ($null -eq $reqLine -or $reqLine -eq '__EXIT__') { break }
    $sepIdx = $reqLine.IndexOf('|')
    if ($sepIdx -lt 0) { continue }
    $tagCsv = $reqLine.Substring(0, $sepIdx)
    $imgPath = $reqLine.Substring($sepIdx + 1).Trim()

    try {
        $ocrEngine = Get-OcrEngineForTags $tagCsv
        if ($ocrEngine -eq $null) {
            Write-Output "NO_LANG"
            continue
        }
        $storageFile = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imgPath)) ([Windows.Storage.StorageFile])
        $stream = Await ($storageFile.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $ocrResult = Await ($ocrEngine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $stream.Dispose()

        $sb = New-Object System.Text.StringBuilder
        [void]$sb.Append("OK")
        foreach ($line in $ocrResult.Lines) {
            if ($line.Words.Count -eq 0) { continue }
            $minX = 999999.0; $minY = 999999.0; $maxX = 0.0; $maxY = 0.0
            foreach ($w in $line.Words) {
                $r = $w.BoundingRect
                if ($r.X -lt $minX) { $minX = $r.X }
                if ($r.Y -lt $minY) { $minY = $r.Y }
                if (($r.X + $r.Width) -gt $maxX) { $maxX = $r.X + $r.Width }
                if (($r.Y + $r.Height) -gt $maxY) { $maxY = $r.Y + $r.Height }
            }
            $txt = $line.Text.Replace("`r", " ").Replace("`n", " ").Replace("`t", " ")
            [void]$sb.Append("`t")
            [void]$sb.Append([int][Math]::Floor($minX))
            [void]$sb.Append(",")
            [void]$sb.Append([int][Math]::Floor($minY))
            [void]$sb.Append(",")
            [void]$sb.Append([int][Math]::Ceiling($maxX))
            [void]$sb.Append(",")
            [void]$sb.Append([int][Math]::Ceiling($maxY))
            [void]$sb.Append("|")
            [void]$sb.Append($txt)
        }
        Write-Output $sb.ToString()
    } catch {
        Write-Output ("ERR|" + $_.Exception.Message)
    }
}
"""


class WindowsNativeOcrProcess:
    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen[str]] = None
        self._script_path: Optional[Path] = None
        self.available_tags: set[str] = set()

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if sys.platform != "win32":
            return

        self._script_path = Path(tempfile.gettempdir()) / f"win_ocr_fast_{os.getpid()}.ps1"
        self._script_path.write_text(POWERSHELL_OCR_SCRIPT, encoding="utf-8-sig")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self._script_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )

        ready_line = self._proc.stdout.readline().strip() if self._proc.stdout else ""
        if not ready_line.startswith("READY|"):
            self.stop()
            return
        tags_str = ready_line.split("|", 1)[1]
        self.available_tags = {t.strip().lower() for t in tags_str.split(",") if t.strip()}

    def supports_lang(self, lang_code: str) -> bool:
        info = LANG_BY_CODE.get(lang_code)
        if not info or not self.available_tags:
            return False
        for tag in info[2]:
            tl = tag.lower()
            if tl in self.available_tags or any(a.startswith(tl.split("-")[0]) for a in self.available_tags):
                return True
        return False

    def recognize(self, image_path: str, lang_code: str) -> Optional[list[tuple[int, int, int, int, str]]]:
        """성공 시 [(x1, y1, x2, y2, text), ...], 해당 언어팩 미설치 시 None 반환."""
        if self._proc is None or self._proc.poll() is not None:
            self.start()
        if self._proc is None or not self._proc.stdin or not self._proc.stdout:
            return None

        info = LANG_BY_CODE.get(lang_code, SUPPORTED_LANGUAGES[0])
        tag_csv = ",".join(info[2])
        abs_path = str(Path(image_path).resolve())

        self._proc.stdin.write(f"{tag_csv}|{abs_path}\n")
        self._proc.stdin.flush()

        resp = self._proc.stdout.readline().rstrip("\r\n")
        if not resp or resp == "NO_LANG":
            return None
        if resp.startswith("ERR|"):
            raise RuntimeError(resp[4:])
        if not resp.startswith("OK"):
            return None

        items: list[tuple[int, int, int, int, str]] = []
        parts = resp.split("\t")[1:]
        for part in parts:
            pipe_idx = part.find("|")
            if pipe_idx <= 0:
                continue
            coords = part[:pipe_idx].split(",")
            if len(coords) != 4:
                continue
            items.append(
                (int(coords[0]), int(coords[1]), int(coords[2]), int(coords[3]), part[pipe_idx + 1 :])
            )
        return items

    def stop(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.stdin and self._proc.poll() is None:
                    self._proc.stdin.write("__EXIT__\n")
                    self._proc.stdin.flush()
                self._proc.terminate()
                self._proc.wait(timeout=1.5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._script_path and self._script_path.exists():
            try:
                self._script_path.unlink()
            except Exception:
                pass


def recognize_with_cloud_ocr(img: Image.Image, lang_code: str) -> list[TextBlock]:
    """Windows 언어 팩이 없어도 동작하는 다국어 고정밀 클라우드 OCR (OCR.space)."""
    info = LANG_BY_CODE.get(lang_code, SUPPORTED_LANGUAGES[0])
    ocr_lang = info[3]
    # Engine 2는 라틴/영어/숫자/특수폰트에 최강, CJK(한/일/중)는 Engine 1 또는 2 지원
    engine_ver = "2" if ocr_lang in ("eng", "spa", "fre", "ger") else "1"

    img_w, img_h = img.size
    max_dim = 1280
    scale = 1.0
    if max(img_w, img_h) > max_dim:
        scale = max_dim / float(max(img_w, img_h))
        send_img = img.resize(
            (max(1, int(img_w * scale)), max(1, int(img_h * scale))),
            Image.Resampling.BILINEAR,
        )
    else:
        send_img = img

    mem = io.BytesIO()
    send_img.convert("RGB").save(mem, format="JPEG", quality=80)
    jpg_bytes = mem.getvalue()

    boundary = "----TransOverlayBoundary887766"
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )

    add_field("language", ocr_lang)
    add_field("isOverlayRequired", "true")
    add_field("OCREngine", engine_ver)
    add_field("scale", "true")
    add_field("detectOrientation", "false")
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="capture.jpg"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n".encode("utf-8")
        + jpg_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(
        "https://api.ocr.space/parse/image",
        data=body,
        headers={
            "apikey": "helloworld",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))

    if data.get("IsErroredOnProcessing"):
        raise RuntimeError(str(data.get("ErrorMessage") or "클라우드 OCR 오류"))

    blocks: list[TextBlock] = []
    for parsed in data.get("ParsedResults") or []:
        overlay = parsed.get("TextOverlay") or {}
        for line in overlay.get("Lines") or []:
            words = line.get("Words") or []
            if not words:
                continue
            line_text = clean_ocr_text(str(line.get("LineText") or ""), lang_code)
            if not line_text or not is_translatable_text(line_text, lang_code):
                continue
            xs1 = [float(w.get("Left", 0)) for w in words]
            ys1 = [float(w.get("Top", 0)) for w in words]
            xs2 = [float(w.get("Left", 0)) + float(w.get("Width", 0)) for w in words]
            ys2 = [float(w.get("Top", 0)) + float(w.get("Height", 0)) for w in words]
            x1 = max(0, int(min(xs1) / scale))
            y1 = max(0, int(min(ys1) / scale))
            x2 = min(img_w - 1, int(max(xs2) / scale))
            y2 = min(img_h - 1, int(max(ys2) / scale))
            if x2 - x1 >= 6 and y2 - y1 >= 6:
                blocks.append(TextBlock(x1, y1, x2, y2, line_text))
    return blocks


# ---------------------------------------------------------------------------
# 텍스트 블록, 일관된 전처리(전체화면 == 영역캡처 동일 결과 보장), Google 번역, 고속 렌더링
# ---------------------------------------------------------------------------

@dataclass
class TextBlock:
    x1: int
    y1: int
    x2: int
    y2: int
    text: str
    translated: str = ""
    last_line_height: int = 0

    def __post_init__(self) -> None:
        if self.last_line_height <= 0:
            self.last_line_height = max(1, self.y2 - self.y1)

    @property
    def width(self) -> int:
        return max(1, self.x2 - self.x1)

    @property
    def height(self) -> int:
        return max(1, self.y2 - self.y1)


def find_target_fonts() -> tuple[Optional[str], Optional[str]]:
    reg_candidates = [
        r"C:\Windows\Fonts\malgun.ttf",
        r"C:\Windows\Fonts\NanumGothic.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    bold_candidates = [
        r"C:\Windows\Fonts\malgunbd.ttf",
        r"C:\Windows\Fonts\NanumGothicBold.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ]
    reg = next((p for p in reg_candidates if os.path.exists(p)), None)
    bold = next((p for p in bold_candidates if os.path.exists(p)), reg)
    return reg, bold


def is_translatable_text(text: str, source_lang: str) -> bool:
    """단일 기호/순수 숫자만 제외하고 선택된 원본 언어의 유의미한 문자가 있는지 판별."""
    s = text.strip()
    if not s:
        return False
    if source_lang in ("en", "es", "fr", "de", "vi", "id"):
        return len(re.findall(r"[A-Za-zÀ-ÿ]", s)) >= 2
    if source_lang == "ru":
        return len(re.findall(r"[А-Яа-яЁё]", s)) >= 2
    if source_lang == "ko":
        return len(re.findall(r"[가-힣]", s)) >= 1
    if source_lang in ("ja", "zh-CN", "zh-TW"):
        return len(re.findall(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", s)) >= 1
    if source_lang == "th":
        return len(re.findall(r"[\u0e00-\u0e7f]", s)) >= 1
    return any(ch.isalpha() for ch in s)


def clean_ocr_text(text: str, source_lang: str) -> str:
    fixed = text.strip()
    fixed = re.sub(r"^[\-\–\—\>\|\•\·\*\+\=\~\^\°\©\®\§\♦\◇\○\●\□\■]+\s*", "", fixed)
    fixed = re.sub(r"\s*[\-\–\—\<\|\•\·\*\~\^\°]+$", "", fixed)
    if source_lang == "en":
        fixed = re.sub(r"^[Oo0]\s+(?=[A-Z][a-z])", "", fixed)
        fixed = re.sub(r"\s+[iI]$", "", fixed)
        fixed = re.sub(r"(?<=[A-Za-z])Q(?=[a-z])", "a", fixed)
        fixed = re.sub(r"(?<=[a-z])Q(?=[A-Za-z])", "a", fixed)
        fixed = re.sub(r"\bQ([a-tv-z])", r"a\1", fixed)
        fixed = fixed.replace("l(", "k").replace("I(", "k").replace("|(", "k")
        fixed = re.sub(r"\bYOLI\b", "you", fixed, flags=re.IGNORECASE)
        fixed = re.sub(r"\bSCOL[il1]t\b|\bSCOUt\b", "Scout", fixed)
        fixed = re.sub(r"\bWeopon\b|\bWeap0n\b", "Weapon", fixed, flags=re.IGNORECASE)
        fixed = re.sub(r"\bRodor\b", "Radar", fixed, flags=re.IGNORECASE)
        fixed = re.sub(r"\bStotion\b", "Station", fixed, flags=re.IGNORECASE)
        fixed = re.sub(r"\borea\b", "area", fixed, flags=re.IGNORECASE)
    return fixed.strip()


def text_quality_score(text: str) -> float:
    cleaned = text.strip()
    if not cleaned:
        return 0.0
    letters = sum(1 for c in cleaned if c.isalpha())
    spaces = sum(1 for c in cleaned if c.isspace())
    weird = sum(1 for c in cleaned if not (c.isalnum() or c in " .,!?'-:/()[]%&+"))
    mid_q_penalty = len(re.findall(r"[a-z]Q[a-z]|[a-z][01][a-z]", cleaned)) * 3.0
    return (letters * 1.4 + spaces * 0.5) - (weird * 3.0) - mid_q_penalty


def preprocess_ocr_consistent(img: Image.Image, turbo_mode: bool = True) -> list[tuple[str, float]]:
    """
    인식률 극대화 + 고속 8-bit BMP 이중 패스 (전체화면 == 영역 캡처 100% 동일 배율):
    - Max-RGB 채널(R/G/B 중 최대 밝기)을 사용해 주황/연두/파랑 등 유채색 게임 글씨가
      어두운 배경에 묻히지 않고 선명하게 살아남도록 처리.
    - 패스 1 (원본 그레이 1.6x BICUBIC): 밝은 탭/버튼/아이템 이미지 위 텍스트 포착
    - 패스 2 (Max-RGB 반전 + 고속 LUT 1.6x BICUBIC + 약한 샤픈): 어두운 게임 UI 회색/유채색 텍스트 포착
    """
    w, h = img.size
    rgb = img.convert("RGB")
    r_ch, g_ch, b_ch = rgb.split()
    max_rgb = ImageChops.lighter(r_ch, ImageChops.lighter(g_ch, b_ch))
    gray = rgb.convert("L")

    thumb = max_rgb.resize((48, 48), Image.Resampling.NEAREST)
    hist = thumb.histogram()
    total = sum(hist)
    mean_luma = ImageStat.Stat(thumb).mean[0]

    acc = 0
    lo, hi = 0, 255
    for i, c in enumerate(hist):
        acc += c
        if acc >= total * 0.01:
            lo = i
            break
    acc = 0
    for i in range(255, -1, -1):
        acc += hist[i]
        if acc >= total * 0.01:
            hi = i
            break
    span = max(24, hi - lo)

    scale = 1.6
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    ts = int(time.time() * 1000)
    tmp_dir = Path(tempfile.gettempdir())
    outputs: list[tuple[str, float]] = []

    # 패스 1: 원본 그레이 1.6x BICUBIC BMP (밝은 배경/아이템 위 글씨)
    p1 = str(tmp_dir / f"ocr_o_{ts}.bmp")
    gray.resize(new_size, Image.Resampling.BICUBIC).save(p1, "BMP")
    outputs.append((p1, scale))

    # 패스 2: Max-RGB 반전 + 부드러운 LUT 대비 확장 + 미세 샤픈 1.6x BMP (어두운 배경 + 유채색 글씨)
    if mean_luma < 165:
        lut = [max(0, min(255, int(255 - (p - lo) * 255.0 / span))) for p in range(256)]
        inv_up = max_rgb.point(lut).resize(new_size, Image.Resampling.BICUBIC)
        inv_up = inv_up.filter(ImageFilter.UnsharpMask(radius=0.8, percent=105, threshold=2))
        p2 = str(tmp_dir / f"ocr_i_{ts}.bmp")
        inv_up.save(p2, "BMP")
        outputs.append((p2, scale))

    return outputs


def _box_iou(a: TextBlock, b: TextBlock) -> float:
    ix1 = max(a.x1, b.x1)
    iy1 = max(a.y1, b.y1)
    ix2 = min(a.x2, b.x2)
    iy2 = min(a.y2, b.y2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    min_area = min(a.width * a.height, b.width * b.height)
    return inter / max(1, min_area)


def combine_multipass_blocks(pass_results: list[list[TextBlock]]) -> list[TextBlock]:
    combined: list[TextBlock] = []
    for blocks in pass_results:
        for cand in blocks:
            matched_idx = -1
            for i, existing in enumerate(combined):
                if _box_iou(existing, cand) >= 0.45:
                    matched_idx = i
                    break
            if matched_idx == -1:
                combined.append(cand)
            elif text_quality_score(cand.text) > text_quality_score(combined[matched_idx].text):
                combined[matched_idx] = cand
    return combined


def _can_merge_lines(existing: TextBlock, blk: TextBlock, max_gap_ratio: float) -> bool:
    h1 = max(1, existing.last_line_height)
    h2 = max(1, blk.last_line_height)
    if max(h1, h2) / min(h1, h2) > 1.35:
        return False
    base_h = min(h1, h2)
    vertical_gap = blk.y1 - existing.y2
    max_gap = min(22.0, base_h * max_gap_ratio)
    if not (-0.25 * base_h <= vertical_gap <= max_gap):
        return False
    left_diff = abs(existing.x1 - blk.x1)
    horiz_overlap = min(existing.x2, blk.x2) - max(existing.x1, blk.x1)
    min_w = min(existing.width, blk.width)
    return left_diff <= max(28, base_h * 1.5) and horiz_overlap > min_w * 0.25


def merge_tight_lines(blocks: list[TextBlock]) -> list[TextBlock]:
    if len(blocks) <= 1:
        return blocks
    sorted_blocks = sorted(blocks, key=lambda b: (b.y1, b.x1))
    merged: list[TextBlock] = []
    for blk in sorted_blocks:
        placed = False
        for existing in merged:
            if _can_merge_lines(existing, blk, max_gap_ratio=0.55):
                existing.x1 = min(existing.x1, blk.x1)
                existing.y1 = min(existing.y1, blk.y1)
                existing.x2 = max(existing.x2, blk.x2)
                existing.y2 = max(existing.y2, blk.y2)
                existing.last_line_height = blk.last_line_height
                existing.text = (
                    existing.text[:-1] + blk.text
                    if existing.text.endswith("-")
                    else f"{existing.text} {blk.text}"
                )
                placed = True
                break
        if not placed:
            merged.append(
                TextBlock(blk.x1, blk.y1, blk.x2, blk.y2, blk.text, last_line_height=blk.last_line_height)
            )
    return merged


# ---------------------------------------------------------------------------
# Google 번역 전용 초고속 엔진 (터보: 초경량 gtx Keep-Alive, 정밀: 최신 웹 batchexecute)
# ---------------------------------------------------------------------------

GAME_UI_GLOSSARY_EN_KO: dict[str, str] = {
    "completed": "완료됨",
    "complete": "완료",
    "claimed": "획득됨",
    "claim": "획득",
    "claim all": "모두 수령",
    "purchase": "구매",
    "buy": "구매",
    "sell": "판매",
    "history": "내역",
    "favorites": "즐겨찾기",
    "recently purchased": "최근 구매",
    "enter item name": "품목명 입력",
    "equipment": "장비",
    "weapon accessories": "무기 액세서리",
    "weapon bolts": "무기 볼트",
    "ammo": "탄약",
    "medical supplies": "의료 용품",
    "tactical items": "전술 용품",
    "keys": "열쇠",
    "miscellaneous": "기타",
    "provisions": "보급품",
    "exchangeable with contact": "연락처로 교환 가능",
    "rewards": "보상",
    "reward": "보상",
    "go now": "지금 가기",
    "storage": "보관함",
    "gunsmith": "총기 개조",
    "contacts": "연락처",
    "market": "마켓",
    "missions": "미션",
    "mission": "미션",
    "events": "이벤트",
    "more events": "더 많은 이벤트",
    "announcements": "공지사항",
    "announcement": "공지사항",
    "check-in riches": "출석 보상",
    "check-in": "출석 체크",
    "season collection": "시즌 컬렉션",
    "fame and fortune": "명성과 부",
    "weekend challenge": "주말 챌린지",
    "limited-time special offer": "기간 한정 특별 혜택",
    "back": "뒤로",
    "confirm": "확인",
    "cancel": "취소",
    "close": "닫기",
    "settings": "설정",
    "inventory": "인벤토리",
    "customization": "커스터마이징",
    "current season": "현재 시즌",
}

_TRANSLATION_CACHE: dict[tuple[str, str, str], str] = {}
_WEB_CONN: Optional[http.client.HTTPSConnection] = None
_WEB_CONN_LOCK = threading.Lock()
_GTX_CONN: Optional[http.client.HTTPSConnection] = None
_GTX_CONN_LOCK = threading.Lock()


def _parse_batchexecute_response(raw: str) -> Optional[str]:
    for line in raw.splitlines():
        s = line.strip()
        if not s.startswith("["):
            continue
        try:
            outer = json.loads(s)
        except Exception:
            continue
        if not isinstance(outer, list):
            continue
        for item in outer:
            if (
                isinstance(item, list)
                and len(item) >= 3
                and item[0] == "wrb.fr"
                and item[1] == "MkEWBc"
                and isinstance(item[2], str)
            ):
                try:
                    inner = json.loads(item[2])
                    segs = inner[1][0][0][5]
                    if isinstance(segs, list) and segs:
                        parts = [
                            str(seg[0])
                            for seg in segs
                            if isinstance(seg, list) and seg and seg[0] is not None
                        ]
                        joined = "".join(parts).strip()
                        if joined:
                            return joined
                except Exception:
                    continue
    return None


def _google_web_nmt_keepalive(text: str, sl: str, tl: str) -> Optional[str]:
    global _WEB_CONN
    inner_req = json.dumps([[text, sl, tl, True], [None]], ensure_ascii=False, separators=(",", ":"))
    outer_req = json.dumps([[["MkEWBc", inner_req, None, "generic"]]], ensure_ascii=False, separators=(",", ":"))
    body = urllib.parse.urlencode({"f.req": outer_req}).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Origin": "https://translate.google.co.kr",
        "Referer": "https://translate.google.co.kr/",
        "Connection": "keep-alive",
    }
    path = "/_/TranslateWebserverUi/data/batchexecute?rpcids=MkEWBc&hl=ko&rt=c"
    with _WEB_CONN_LOCK:
        for attempt in range(2):
            try:
                if _WEB_CONN is None:
                    _WEB_CONN = http.client.HTTPSConnection("translate.google.co.kr", timeout=6)
                _WEB_CONN.request("POST", path, body=body, headers=headers)
                resp = _WEB_CONN.getresponse()
                raw = resp.read().decode("utf-8", errors="ignore")
                if resp.status == 200:
                    return _parse_batchexecute_response(raw)
                raise RuntimeError(f"HTTP {resp.status}")
            except Exception:
                try:
                    if _WEB_CONN is not None:
                        _WEB_CONN.close()
                except Exception:
                    pass
                _WEB_CONN = None
                if attempt == 1:
                    return None
    return None


def _gtx_keepalive_get(query_str: str) -> list:
    global _GTX_CONN
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Connection": "keep-alive",
    }
    path = f"/translate_a/single?{query_str}"
    with _GTX_CONN_LOCK:
        for attempt in range(2):
            try:
                if _GTX_CONN is None:
                    _GTX_CONN = http.client.HTTPSConnection("translate.googleapis.com", timeout=5)
                _GTX_CONN.request("GET", path, headers=headers)
                resp = _GTX_CONN.getresponse()
                raw = resp.read().decode("utf-8", errors="ignore")
                if resp.status == 200:
                    return json.loads(raw)
                raise RuntimeError(f"HTTP {resp.status}")
            except Exception:
                try:
                    if _GTX_CONN is not None:
                        _GTX_CONN.close()
                except Exception:
                    pass
                _GTX_CONN = None
                if attempt == 1:
                    raise
    return []


def prewarm_google_connection() -> None:
    try:
        q = urllib.parse.urlencode([("client", "gtx"), ("sl", "en"), ("tl", "ko"), ("dt", "t"), ("q", "ok")])
        _gtx_keepalive_get(q)
    except Exception:
        pass
    try:
        _google_web_nmt_keepalive("ok", "en", "ko")
    except Exception:
        pass


def _lookup_glossary(text: str, sl: str, tl: str) -> Optional[str]:
    if sl != "en" or tl != "ko":
        return None
    stripped = text.strip()
    m = re.match(r"^([(\[\{<\s]*)(.*?)([)\]\}>\s]*)$", stripped)
    if not m:
        return None
    prefix, core, suffix = m.group(1), m.group(2).strip(), m.group(3)
    key = core.lower()
    if key in GAME_UI_GLOSSARY_EN_KO:
        return f"{prefix}{GAME_UI_GLOSSARY_EN_KO[key]}{suffix}"
    # 패턴형 게임 UI (Stage 1 -> 1단계, Level 5 -> 레벨 5 등)
    sm = re.match(r"^stage\s+(\d+)$", key)
    if sm:
        return f"{prefix}{sm.group(1)}단계{suffix}"
    return None


def _translate_one_google(text: str, sl: str, tl: str, turbo_mode: bool) -> str:
    # 1순위: 사전 워밍업된 Keep-Alive 최신 Google 웹 NMT 엔진 (왕복 1회, 최고 품질)
    res = _google_web_nmt_keepalive(text, sl, tl)
    if res:
        return res
    # 2순위: gtx Keep-Alive 폴백 (자동 철자 교정 dt=qca 포함)
    query = urllib.parse.urlencode(
        [("client", "gtx"), ("sl", sl), ("tl", tl), ("dt", "t"), ("dt", "qca"), ("q", text)]
    )
    payload = _gtx_keepalive_get(query)
    if isinstance(payload, list) and payload and isinstance(payload[0], list):
        return "".join(
            seg[0] for seg in payload[0] if isinstance(seg, list) and seg and seg[0]
        ).strip()
    return text


def _translate_chunk_anchored(chunk: list[str], sl: str, tl: str, turbo_mode: bool) -> list[str]:
    """소배치(최대 12개)를 인덱스 앵커([[0]], [[1]]...)로 묶어 번역해 일부 누락/밀림을 원천 차단."""
    if not chunk:
        return []
    if len(chunk) == 1:
        try:
            return [_translate_one_google(chunk[0], sl, tl, turbo_mode)]
        except Exception:
            return [chunk[0]]

    tagged_lines = [f"[[{idx}]] {txt}" for idx, txt in enumerate(chunk)]
    joined = "\n".join(tagged_lines)
    mapped: dict[int, str] = {}

    try:
        ko_joined = _translate_one_google(joined, sl, tl, turbo_mode)
        # [[0]] ... [[1]] ... 패턴으로 각 인덱스별 번역 정밀 추출
        matches = list(re.finditer(r"\[\s*\[\s*(\d+)\s*\]\s*\]", ko_joined))
        for pos, m in enumerate(matches):
            idx = int(m.group(1))
            val_start = m.end()
            val_end = matches[pos + 1].start() if pos + 1 < len(matches) else len(ko_joined)
            val = re.sub(r"\s+", " ", ko_joined[val_start:val_end]).strip()
            if 0 <= idx < len(chunk) and val:
                mapped[idx] = val
    except Exception:
        pass

    out: list[str] = []
    for idx, orig in enumerate(chunk):
        if idx in mapped:
            out.append(mapped[idx])
        else:
            # 혹시 누락된 항목만 개별 즉시 재시도
            try:
                out.append(_translate_one_google(orig, sl, tl, turbo_mode))
            except Exception:
                out.append(orig)
    return out


def translate_with_google_fast(
    texts: list[str], sl: str = "en", tl: str = "ko", turbo_mode: bool = True
) -> list[str]:
    results: list[Optional[str]] = [None] * len(texts)
    missing_indices: list[int] = []
    missing_texts: list[str] = []

    for i, t in enumerate(texts):
        gloss = _lookup_glossary(t, sl, tl)
        if gloss is not None:
            results[i] = gloss
            continue
        cache_key = (sl, tl, t.strip())
        cached = _TRANSLATION_CACHE.get(cache_key)
        if cached is not None:
            results[i] = cached
            continue
        missing_indices.append(i)
        missing_texts.append(t)

    if not missing_texts:
        return [r or "" for r in results]

    # 12개 단위 소배치 분할 후 최대 3스레드 병렬 실행 -> 전체화면 대량 텍스트도 누락 없이 초고속!
    chunk_size = 12
    chunks = [
        (missing_indices[i : i + chunk_size], missing_texts[i : i + chunk_size])
        for i in range(0, len(missing_texts), chunk_size)
    ]

    if len(chunks) == 1:
        idxs, txts = chunks[0]
        ko_list = _translate_chunk_anchored(txts, sl, tl, turbo_mode)
        for idx, orig_t, ko_t in zip(idxs, txts, ko_list):
            results[idx] = ko_t
            _TRANSLATION_CACHE[(sl, tl, orig_t.strip())] = ko_t
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(chunks))) as pool:
            futs = [
                (idxs, txts, pool.submit(_translate_chunk_anchored, txts, sl, tl, turbo_mode))
                for idxs, txts in chunks
            ]
            for idxs, txts, fut in futs:
                ko_list = fut.result()
                for idx, orig_t, ko_t in zip(idxs, txts, ko_list):
                    results[idx] = ko_t
                    _TRANSLATION_CACHE[(sl, tl, orig_t.strip())] = ko_t

    return [r if r is not None else t for r, t in zip(results, texts)]


# ---------------------------------------------------------------------------
# 자연스러운 칼럼별 인페인팅 + 글자 잘림 0% + 전체 복사 없는 초고속 렌더링
# ---------------------------------------------------------------------------

def _rgb_dist_sq(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> int:
    return (c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2


def _fast_median_rgb(pixels: list[tuple[int, int, int]], fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    n = len(pixels)
    if n == 0:
        return fallback
    mid = n // 2
    rs = sorted(p[0] for p in pixels)
    gs = sorted(p[1] for p in pixels)
    bs = sorted(p[2] for p in pixels)
    return (rs[mid], gs[mid], bs[mid])


def extract_bg_and_fg_colors(
    img: Image.Image, px, x1: int, y1: int, x2: int, y2: int
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    w, h = img.size
    pad = 3
    top_y = max(0, y1 - pad)
    bot_y = min(h - 1, y2 + pad)
    lx = max(0, x1 - pad)
    rx = min(w - 1, x2 + pad)

    step_x = max(1, (rx - lx) // 12)
    border_px = [px[x, top_y][:3] for x in range(lx, rx + 1, step_x)]
    border_px += [px[x, bot_y][:3] for x in range(lx, rx + 1, step_x)]
    avg_bg = _fast_median_rgb(border_px, (24, 26, 30))

    inner_samples: list[tuple[int, tuple[int, int, int]]] = []
    sx = max(1, (x2 - x1) // 14)
    sy = max(1, (y2 - y1) // 7)
    for yy in range(max(0, y1), min(h, y2 + 1), sy):
        for xx in range(max(0, x1), min(w, x2 + 1), sx):
            rgb = px[xx, yy][:3]
            inner_samples.append((_rgb_dist_sq(rgb, avg_bg), rgb))

    inner_samples.sort(key=lambda item: item[0], reverse=True)
    top_count = max(3, len(inner_samples) // 8)
    fg_candidates = [rgb for dist, rgb in inner_samples[:top_count] if dist > 500]

    bg_luma = 0.299 * avg_bg[0] + 0.587 * avg_bg[1] + 0.114 * avg_bg[2]
    if fg_candidates:
        fg_rgb = _fast_median_rgb(fg_candidates, (228, 230, 235))
    else:
        fg_rgb = (228, 230, 235) if bg_luma < 140 else (18, 22, 30)

    fg_luma = 0.299 * fg_rgb[0] + 0.587 * fg_rgb[1] + 0.114 * fg_rgb[2]
    if bg_luma < 120 and fg_luma < 175:
        boost = 185.0 / max(40.0, fg_luma)
        fg_rgb = (
            min(248, int(fg_rgb[0] * boost)),
            min(248, int(fg_rgb[1] * boost)),
            min(248, int(fg_rgb[2] * boost)),
        )
    elif bg_luma >= 140 and fg_luma > 90:
        fg_rgb = (18, 22, 30)

    return avg_bg, fg_rgb


def _safe_textlength(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    """줄바꿈(\n)이 포함된 문자열에서도 ValueError 없이 최대 줄 길이를 안전하게 측정."""
    if "\n" not in text and "\r" not in text:
        return float(draw.textlength(text, font=font))
    lines = text.splitlines()
    if not lines:
        return 0.0
    return max(float(draw.textlength(line, font=font)) for line in lines)


def wrap_text_to_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    if max_width <= 12:
        return [text]
    if "\n" not in text and _safe_textlength(draw, text, font=font) <= max_width:
        return [text]

    lines: list[str] = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            continue
        if _safe_textlength(draw, paragraph, font=font) <= max_width:
            lines.append(paragraph)
            continue
        current = ""
        for ch in paragraph:
            test = current + ch
            if _safe_textlength(draw, test, font=font) <= max_width or not current:
                current = test
            else:
                lines.append(current.rstrip())
                current = ch.lstrip()
        if current:
            lines.append(current.rstrip())
    return lines or [text]


def render_natural_translated_image(
    orig_img: Image.Image,
    blocks: list[TextBlock],
    reg_font_path: Optional[str],
    bold_font_path: Optional[str],
) -> Image.Image:
    """전체 이미지 복사 없이 위/아래 스트립만 먼저 추출한 뒤 초고속 칼럼 인페인팅 & 무잘림 렌더링."""
    img = orig_img.convert("RGB")
    px = img.load()
    img_w, img_h = img.size

    font_cache: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}

    def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        size = max(9, min(120, size))
        key = (bold, size)
        if key not in font_cache:
            path = bold_font_path if bold else reg_font_path
            if path:
                font_cache[key] = ImageFont.truetype(path, size)
            else:
                font_cache[key] = ImageFont.load_default()
        return font_cache[key]

    # 1단계: 원본 수정 전에 모든 박스의 색상 및 상하 배경 스트립만 가볍게 수집 (전체 6MB 복사 제거!)
    prep_data: list[tuple[tuple[int, int, int], tuple[int, int, int], Optional[tuple[int, int, int, int, Image.Image, Image.Image]]]] = []
    for blk in blocks:
        if not blk.translated:
            prep_data.append(((24, 26, 30), (228, 230, 235), None))
            continue
        avg_bg, fg_rgb = extract_bg_and_fg_colors(img, px, blk.x1, blk.y1, blk.x2, blk.y2)

        pad_x = max(5, min(10, int(blk.height * 0.15)))
        pad_y = max(3, min(7, int(blk.height * 0.12)))
        rx1 = max(0, blk.x1 - pad_x)
        ry1 = max(0, blk.y1 - pad_y)
        rx2 = min(img_w, blk.x2 + pad_x)
        ry2 = min(img_h, blk.y2 + pad_y)
        if rx2 - rx1 > 2 and ry2 - ry1 > 2:
            top_y = max(0, ry1 - 2)
            bot_y = min(img_h - 1, ry2 + 1)
            top_strip = img.crop((rx1, top_y, rx2, top_y + 1))
            bot_strip = img.crop((rx1, bot_y, rx2, bot_y + 1))
            prep_data.append((avg_bg, fg_rgb, (rx1, ry1, rx2 - rx1, ry2 - ry1, top_strip, bot_strip)))
        else:
            prep_data.append((avg_bg, fg_rgb, None))

    # 2단계: 칼럼별 배경 인페인팅 적용
    for _, _, strip_info in prep_data:
        if strip_info is None:
            continue
        rx1, ry1, rw, rh, top_strip, bot_strip = strip_info
        grad_seed = Image.new("RGB", (rw, 2))
        grad_seed.paste(top_strip, (0, 0))
        grad_seed.paste(bot_strip, (0, 1))
        patch = grad_seed.resize((rw, rh), Image.Resampling.BILINEAR)

        mask = Image.new("L", (rw, rh), 0)
        mdraw = ImageDraw.Draw(mask)
        inset = 2 if min(rw, rh) > 10 else 0
        mdraw.rounded_rectangle(
            [inset, inset, max(inset, rw - 1 - inset), max(inset, rh - 1 - inset)],
            radius=4,
            fill=255,
        )
        mask = mask.filter(ImageFilter.GaussianBlur(radius=1.6))
        img.paste(patch, (rx1, ry1), mask)

    # 3단계: 경계 클램핑 + TrueType 오프셋 보정 무잘림 렌더링
    draw = ImageDraw.Draw(img)
    for blk, (avg_bg, fg_rgb, _) in zip(blocks, prep_data):
        if not blk.translated:
            continue
        clean_tr = re.sub(r"\s+", " ", blk.translated).strip()
        if not clean_tr:
            continue
        blk.translated = clean_tr

        fg_luma = 0.299 * fg_rgb[0] + 0.587 * fg_rgb[1] + 0.114 * fg_rgb[2]
        use_bold = fg_luma > 215 or blk.height >= 26

        is_single_line_src = (blk.height <= blk.last_line_height * 1.45)
        max_right_w = max(20, img_w - blk.x1 - 4)
        avail_w = max(20, min(blk.width + 6, max_right_w))
        max_bottom_h = max(14, img_h - blk.y1 - 4)
        avail_h = max(14, min(int(blk.height * 1.18) + 4, max_bottom_h))

        if is_single_line_src:
            target_size = min(120, max(10, int(blk.height * 0.88)))
            best_font = get_font(target_size, use_bold)
            best_lines = [blk.translated]
            best_line_h = target_size + 2

            min_single_size = max(10, int(blk.height * 0.58))
            fitted_single = False
            for size in range(target_size, min_single_size - 1, -1):
                font = get_font(size, use_bold)
                if _safe_textlength(draw, blk.translated, font=font) <= avail_w:
                    tb = draw.textbbox((0, 0), blk.translated, font=font)
                    best_font = font
                    best_lines = [blk.translated]
                    best_line_h = max(size + 2, (tb[3] - tb[1]) + 3)
                    fitted_single = True
                    break

            if not fitted_single:
                wrap_start = min(target_size, max(11, int(blk.height * 0.72)))
                for size in range(wrap_start, 8, -1):
                    font = get_font(size, use_bold)
                    lines = wrap_text_to_width(draw, blk.translated, font, avail_w)
                    sb = draw.textbbox((0, 0), "가Ag", font=font)
                    line_h = max(size + 2, (sb[3] - sb[1]) + 3)
                    if line_h * len(lines) <= min(int(avail_h * 1.25), max_bottom_h):
                        best_font = font
                        best_lines = lines
                        best_line_h = line_h
                        break
        else:
            target_size = min(48, max(10, int(blk.last_line_height * 0.88)))
            best_font = get_font(target_size, use_bold)
            best_lines = [blk.translated]
            best_line_h = target_size + 3

            for size in range(target_size, 8, -1):
                font = get_font(size, use_bold)
                lines = wrap_text_to_width(draw, blk.translated, font, avail_w)
                sb = draw.textbbox((0, 0), "가Ag", font=font)
                line_h = max(size + 2, (sb[3] - sb[1]) + 3)
                if line_h * len(lines) <= min(avail_h * 1.25, max_bottom_h):
                    best_font = font
                    best_lines = lines
                    best_line_h = line_h
                    break
            else:
                best_font = get_font(9, use_bold)
                best_lines = wrap_text_to_width(draw, blk.translated, best_font, avail_w)
                best_line_h = 12

        total_h = best_line_h * len(best_lines)
        cur_y = blk.y1 + (blk.height - total_h) // 2
        cur_y = max(2, min(img_h - total_h - 3, cur_y))

        bg_luma = 0.299 * avg_bg[0] + 0.587 * avg_bg[1] + 0.114 * avg_bg[2]
        shadow_rgb = (0, 0, 0) if bg_luma < 125 else None
        center_single = is_single_line_src and len(best_lines) == 1 and blk.width <= 220

        for line in best_lines:
            lb = draw.textbbox((0, 0), line, font=best_font)
            lw = lb[2] - lb[0]
            y_off = lb[1]
            tx = blk.x1 + (blk.width - lw) // 2 if center_single else blk.x1
            tx = max(2, min(img_w - lw - 3, tx))
            draw_y = max(1, cur_y - y_off)
            if shadow_rgb is not None:
                draw.text((tx + 1, draw_y + 1), line, fill=shadow_rgb, font=best_font)
            draw.text((tx, draw_y), line, fill=fg_rgb, font=best_font)
            cur_y += best_line_h

    return img


def qpixmap_to_pil(pixmap: QPixmap) -> Image.Image:
    qimg = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB888)
    w, h = qimg.width(), qimg.height()
    bpl = qimg.bytesPerLine()
    ptr = qimg.bits()
    ptr.setsize(qimg.sizeInBytes())
    return Image.frombuffer("RGB", (w, h), bytes(ptr), "raw", "RGB", bpl, 1).copy()


def pil_to_qpixmap(pil_img: Image.Image) -> QPixmap:
    rgb = pil_img.convert("RGB")
    w, h = rgb.size
    raw = rgb.tobytes("raw", "RGB")
    qimg = QImage(raw, w, h, w * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


# ---------------------------------------------------------------------------
# 백그라운드 번역 워커
# ---------------------------------------------------------------------------

class FastGoogleTranslateWorker(QObject):
    status_changed = pyqtSignal(str)
    ready_changed = pyqtSignal(str)
    translation_finished = pyqtSignal(object, object, QRect, float)
    translation_failed = pyqtSignal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._tasks: queue.Queue[Optional[tuple[Image.Image, QRect, AppConfig]]] = queue.Queue()
        self._stop_event = threading.Event()
        self._ocr = WindowsNativeOcrProcess()
        self._reg_font, self._bold_font = find_target_fonts()
        self._thread = threading.Thread(target=self._run_loop, name="GoogleTranslateWorker", daemon=True)
        self._thread.start()

    def submit(self, orig_img: Image.Image, target_rect: QRect, config: AppConfig) -> None:
        self._tasks.put((orig_img, target_rect, config))

    def shutdown(self) -> None:
        self._stop_event.set()
        self._tasks.put(None)
        self._ocr.stop()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        try:
            self._ocr.start()
            self.ready_changed.emit("준비 완료")
        except Exception:
            self.ready_changed.emit("준비 완료")

        while not self._stop_event.is_set():
            item = self._tasks.get()
            if item is None:
                break
            orig_img, target_rect, cfg = item
            t0 = time.perf_counter()
            try:
                translated_img = self._process(orig_img, cfg)
                elapsed = time.perf_counter() - t0
                self.translation_finished.emit(orig_img, translated_img, target_rect, elapsed)
            except Exception as exc:
                self.translation_failed.emit(f"번역 처리 중 오류가 발생했습니다.\n\n{exc}")

        self._ocr.stop()

    def _process(self, orig_img: Image.Image, cfg: AppConfig) -> Image.Image:
        sl, tl = cfg.source_lang, cfg.target_lang
        blocks: list[TextBlock] = []

        # 해당 언어의 Windows OCR 팩이 있으면 초고속 로컬 OCR, 없으면 자동으로 클라우드 OCR 사용 (언어팩 설치 불필요!)
        if self._ocr.supports_lang(sl):
            self.status_changed.emit("텍스트 인식 중...")
            blocks = self._run_local_ocr(orig_img, sl, cfg.turbo_mode)
        else:
            self.status_changed.emit("클라우드 OCR 인식 중...")
            blocks = recognize_with_cloud_ocr(orig_img, sl)

        if not blocks:
            raise RuntimeError("캡처된 영역에서 번역할 텍스트를 찾지 못했습니다.")

        blocks = merge_tight_lines(blocks)

        self.status_changed.emit("Google 번역 중...")
        translated_list = translate_with_google_fast(
            [b.text for b in blocks], sl=sl, tl=tl, turbo_mode=cfg.turbo_mode
        )
        for blk, tr in zip(blocks, translated_list):
            blk.translated = tr

        return render_natural_translated_image(orig_img, blocks, self._reg_font, self._bold_font)

    def _run_local_ocr(self, orig_img: Image.Image, sl: str, turbo_mode: bool) -> list[TextBlock]:
        prep_list = preprocess_ocr_consistent(orig_img, turbo_mode=turbo_mode)
        pass_blocks: list[list[TextBlock]] = []
        try:
            for prep_path, scale in prep_list:
                raw_lines = self._ocr.recognize(prep_path, sl)
                if raw_lines is None:
                    # 런타임에 언어팩 미지원 확인 시 클라우드 OCR 즉시 폴백
                    return recognize_with_cloud_ocr(orig_img, sl)
                cur: list[TextBlock] = []
                for x1, y1, x2, y2, raw_text in raw_lines:
                    text = clean_ocr_text(raw_text, sl)
                    if not text or not is_translatable_text(text, sl):
                        continue
                    cur.append(
                        TextBlock(
                            int(x1 / scale),
                            int(y1 / scale),
                            int(x2 / scale),
                            int(y2 / scale),
                            text,
                        )
                    )
                pass_blocks.append(cur)
        finally:
            for prep_path, _ in prep_list:
                if os.path.exists(prep_path):
                    try:
                        os.remove(prep_path)
                    except Exception:
                        pass
        return combine_multipass_blocks(pass_blocks)


# ---------------------------------------------------------------------------
# 로딩 토스트 / 영역 선택 / 분리형 외부 툴바 / 오버레이
# ---------------------------------------------------------------------------

class LoadingToast(QWidget):
    """비모달 상태/오류 알림 토스트 (모달 QMessageBox로 인한 캡처 먹통 증상 완전 제거)."""

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)

        self._label = QLabel("번역 중...")
        layout.addWidget(self._label)
        self._auto_hide_timer = QTimer(self)
        self._auto_hide_timer.setSingleShot(True)
        self._auto_hide_timer.timeout.connect(self.hide)
        self._set_style(is_error=False)
        self.adjustSize()

    def _set_style(self, is_error: bool) -> None:
        if is_error:
            self._label.setStyleSheet(
                "QLabel { color: #FECACA; font-size: 12px; font-weight: 600; "
                "background-color: rgba(45, 18, 22, 242); border: 1px solid #991B1B; "
                "border-radius: 8px; padding: 8px 14px; }"
            )
        else:
            self._label.setStyleSheet(
                "QLabel { color: #F4F4F5; font-size: 12px; font-weight: 600; "
                "background-color: rgba(24, 24, 27, 238); border: 1px solid #3F3F46; "
                "border-radius: 8px; padding: 8px 14px; }"
            )

    def _reposition(self) -> None:
        self.adjustSize()
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.right() - self.width() - 20, geo.bottom() - self.height() - 20)

    def show_message(self, message: str) -> None:
        self._auto_hide_timer.stop()
        self._set_style(is_error=False)
        self._label.setText(message)
        self._reposition()
        self.show()
        apply_game_topmost_window(int(self.winId()))

    def show_notice(self, message: str, duration_ms: int = 2800) -> None:
        self._auto_hide_timer.stop()
        self._set_style(is_error=True)
        self._label.setText(message)
        self._reposition()
        self.show()
        apply_game_topmost_window(int(self.winId()))
        self._auto_hide_timer.start(duration_ms)


class RegionSelectorOverlay(QWidget):
    region_selected = pyqtSignal(QPixmap, QRect)
    selection_cancelled = pyqtSignal()

    def __init__(self, screenshot: QPixmap, screen_geo: QRect) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self._screenshot = screenshot
        self._screen_geo = screen_geo
        self._origin: Optional[QPoint] = None
        self._current: Optional[QPoint] = None

        self.setGeometry(screen_geo)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_game_topmost_window(int(self.winId()))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self._screenshot)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))

        painter.setPen(QColor("#F4F4F5"))
        painter.setFont(QFont("Malgun Gothic", 11, QFont.Weight.Bold))
        painter.drawText(
            QRect(0, 20, self.width(), 32),
            Qt.AlignmentFlag.AlignCenter,
            "번역할 영역을 드래그하세요 (ESC / 우클릭: 취소)",
        )

        if self._origin and self._current:
            sel = QRect(self._origin, self._current).normalized()
            if sel.width() > 2 and sel.height() > 2:
                dpr = self._screenshot.devicePixelRatio()
                src_rect = QRect(
                    int(sel.x() * dpr),
                    int(sel.y() * dpr),
                    int(sel.width() * dpr),
                    int(sel.height() * dpr),
                )
                painter.drawPixmap(sel, self._screenshot, src_rect)
                painter.setPen(QPen(QColor("#E4E4E7"), 1.5))
                painter.drawRect(sel)

                size_text = f"{sel.width()} × {sel.height()}"
                badge_y = sel.top() - 26 if sel.top() > 30 else sel.bottom() + 6
                badge_rect = QRect(sel.left(), badge_y, 96, 22)
                painter.fillRect(badge_rect, QColor(24, 24, 27, 225))
                painter.setPen(QColor("#F4F4F5"))
                painter.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
                painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, size_text)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.close()
            self.selection_cancelled.emit()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._origin is not None:
            sel = QRect(self._origin, event.position().toPoint()).normalized()
            self.close()
            if sel.width() < 15 or sel.height() < 15:
                self.selection_cancelled.emit()
                return

            dpr = self._screenshot.devicePixelRatio()
            crop_rect = QRect(
                int(sel.x() * dpr),
                int(sel.y() * dpr),
                int(sel.width() * dpr),
                int(sel.height() * dpr),
            )
            cropped = self._screenshot.copy(crop_rect)
            global_rect = QRect(
                self._screen_geo.x() + sel.x(),
                self._screen_geo.y() + sel.y(),
                sel.width(),
                sel.height(),
            )
            self.region_selected.emit(cropped, global_rect)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            self.selection_cancelled.emit()


class OverlayToolbarWindow(QWidget):
    def __init__(
        self,
        on_toggle: Callable[[], None],
        on_save: Callable[[], None],
        on_close: Callable[[], None],
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        frame = QFrame(self)
        frame.setStyleSheet(
            "QFrame { background-color: rgba(24, 24, 27, 240); "
            "border-radius: 8px; border: 1px solid #3F3F46; }"
            "QPushButton { color: #E4E4E7; background-color: #27272A; "
            "border: 1px solid #3F3F46; border-radius: 5px; "
            "padding: 4px 10px; font-size: 11px; font-weight: 600; }"
            "QPushButton:hover { background-color: #3F3F46; color: #FFFFFF; }"
        )
        tb_layout = QHBoxLayout(frame)
        tb_layout.setContentsMargins(6, 4, 6, 4)
        tb_layout.setSpacing(5)

        self.toggle_btn = QPushButton("원본 비교")
        self.toggle_btn.clicked.connect(on_toggle)
        tb_layout.addWidget(self.toggle_btn)

        save_btn = QPushButton("저장")
        save_btn.clicked.connect(on_save)
        tb_layout.addWidget(save_btn)

        close_btn = QPushButton("닫기")
        close_btn.setStyleSheet(
            "QPushButton { color: #FAFAFA; background-color: #DC2626; "
            "border: none; border-radius: 5px; padding: 4px 10px; font-size: 11px; font-weight: 600; }"
            "QPushButton:hover { background-color: #EF4444; }"
        )
        close_btn.clicked.connect(on_close)
        tb_layout.addWidget(close_btn)

        outer.addWidget(frame)
        self.adjustSize()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_game_topmost_window(int(self.winId()))

    def position_near(self, target_rect: QRect) -> None:
        self.adjustSize()
        tb_w, tb_h = self.width(), self.height()
        screen = QApplication.screenAt(target_rect.center()) or QApplication.primaryScreen()
        s_geo = screen.geometry() if screen else QRect(0, 0, 1920, 1080)

        is_fullscreen = (
            target_rect.width() >= s_geo.width() - 20
            and target_rect.height() >= s_geo.height() - 20
        )

        if is_fullscreen:
            x = s_geo.right() - tb_w - 14
            y = s_geo.top() + 12
        else:
            gap = 10
            x = target_rect.right() - tb_w + 1
            x = max(s_geo.left() + 6, min(s_geo.right() - tb_w - 6, x))
            if target_rect.top() - tb_h - gap >= s_geo.top() + 4:
                y = target_rect.top() - tb_h - gap
            elif target_rect.bottom() + tb_h + gap <= s_geo.bottom() - 4:
                y = target_rect.bottom() + gap
            else:
                y = max(s_geo.top() + 8, target_rect.top())
                if target_rect.right() + tb_w + gap <= s_geo.right() - 4:
                    x = target_rect.right() + gap
                elif target_rect.left() - tb_w - gap >= s_geo.left() + 4:
                    x = target_rect.left() - tb_w - gap

        self.move(x, y)


class TranslatedOverlayWindow(QWidget):
    global_click_detected = pyqtSignal(int, int)

    def __init__(
        self,
        original_img: Image.Image,
        translated_img: Image.Image,
        target_rect: QRect,
        opacity_percent: int = 100,
        close_on_click: bool = True,
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._original_img = original_img
        self._translated_img = translated_img
        self._original_pixmap = pil_to_qpixmap(original_img)
        self._translated_pixmap = pil_to_qpixmap(translated_img)
        self._showing_original = False
        self._opacity = max(20, min(100, opacity_percent))
        self._close_on_click = close_on_click
        self._shown_at = time.monotonic()
        self._mouse_listener: Optional[pynput_mouse.Listener] = None

        self.setGeometry(target_rect)
        self.setWindowOpacity(self._opacity / 100.0)
        self.setMouseTracking(True)

        self._image_label = QLabel(self)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setGeometry(0, 0, target_rect.width(), target_rect.height())
        self._image_label.setStyleSheet("border: none; background-color: #000000;")
        self._render_pixmap()

        self._toolbar_win = OverlayToolbarWindow(
            on_toggle=self.toggle_original,
            on_save=self._save_translated_image,
            on_close=self.close,
        )
        self._toolbar_win.position_near(target_rect)

        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._keep_topmost)
        self._topmost_timer.start(400)

        self.global_click_detected.connect(self._handle_global_click)
        if self._close_on_click:
            self._start_global_mouse_listener()

    def _keep_topmost(self) -> None:
        apply_game_topmost_window(int(self.winId()))
        if self._toolbar_win.isVisible():
            apply_game_topmost_window(int(self._toolbar_win.winId()))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._shown_at = time.monotonic()
        apply_game_topmost_window(int(self.winId()))
        self._toolbar_win.position_near(self.geometry())
        self._toolbar_win.show()
        apply_game_topmost_window(int(self._toolbar_win.winId()))

    def _start_global_mouse_listener(self) -> None:
        def on_click(x: int, y: int, button, pressed: bool) -> None:
            if pressed:
                self.global_click_detected.emit(int(x), int(y))

        try:
            self._mouse_listener = pynput_mouse.Listener(on_click=on_click)
            self._mouse_listener.daemon = True
            self._mouse_listener.start()
        except Exception:
            self._mouse_listener = None

    def _stop_global_mouse_listener(self) -> None:
        if self._mouse_listener is not None:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
            self._mouse_listener = None

    def _handle_global_click(self, gx: int, gy: int) -> None:
        if time.monotonic() - self._shown_at < 0.25:
            return
        if self._toolbar_win.isVisible() and self._toolbar_win.geometry().contains(QCursor.pos()):
            return
        self.close()

    def _render_pixmap(self) -> None:
        src = self._original_pixmap if self._showing_original else self._translated_pixmap
        if not src.isNull():
            dpr = max(1.0, float(self.devicePixelRatioF()))
            target_w = max(1, int(round(self.width() * dpr)))
            target_h = max(1, int(round(self.height() * dpr)))
            scaled = src.scaled(
                target_w,
                target_h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(dpr)
            self._image_label.setPixmap(scaled)

    def toggle_original(self) -> None:
        self._showing_original = not self._showing_original
        self._toolbar_win.toggle_btn.setText("번역 보기" if self._showing_original else "원본 비교")
        self._render_pixmap()

    def _save_translated_image(self) -> None:
        self._stop_global_mouse_listener()
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "번역된 이미지 저장",
            str(Path.home() / "Desktop" / "translated_image.png"),
            "PNG Image (*.png);;JPEG Image (*.jpg)",
        )
        if save_path:
            try:
                if save_path.lower().endswith(".jpg"):
                    self._translated_img.convert("RGB").save(save_path, "JPEG", quality=92)
                else:
                    self._translated_img.save(save_path, "PNG")
            except Exception as exc:
                QMessageBox.warning(self, "저장 실패", f"이미지 저장 중 오류: {exc}")
        if self._close_on_click and self.isVisible():
            self._shown_at = time.monotonic()
            self._start_global_mouse_listener()

    def resizeEvent(self, event) -> None:
        self._image_label.setGeometry(0, 0, self.width(), self.height())
        self._render_pixmap()
        super().resizeEvent(event)

    def wheelEvent(self, event) -> None:
        delta = 5 if event.angleDelta().y() > 0 else -5
        self._opacity = max(20, min(100, self._opacity + delta))
        self.setWindowOpacity(self._opacity / 100.0)

    def mousePressEvent(self, event) -> None:
        if self._close_on_click:
            self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        elif event.key() == Qt.Key.Key_Space:
            self.toggle_original()

    def closeEvent(self, event) -> None:
        self._topmost_timer.stop()
        self._stop_global_mouse_listener()
        try:
            self._toolbar_win.close()
        except Exception:
            pass
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# 절제된 네이티브 다크 UI (Windows 11 / Linear / PowerToys 스타일 — 그라데이션·네온 제거)
# ---------------------------------------------------------------------------

CLEAN_NATIVE_STYLESHEET = """
QMainWindow, QWidget#RootCanvas {
    background-color: #18181B;
    color: #F4F4F5;
    font-family: "Malgun Gothic", "Segoe UI", sans-serif;
}
QFrame#Panel {
    background-color: #202024;
    border: 1px solid #2E2E34;
    border-radius: 10px;
}
QFrame#Divider {
    background-color: #2E2E34;
    max-height: 1px;
}
QLabel#AppTitle {
    font-size: 16px;
    font-weight: 700;
    color: #FAFAFA;
}
QLabel#SubText {
    font-size: 12px;
    color: #A1A1AA;
}
QLabel#GroupLabel {
    font-size: 12px;
    font-weight: 700;
    color: #D4D4D8;
}
QLabel#FieldLabel {
    font-size: 12.5px;
    color: #A1A1AA;
}
QComboBox {
    background-color: #18181B;
    color: #F4F4F5;
    border: 1px solid #3F3F46;
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12.5px;
    min-height: 22px;
}
QComboBox:hover {
    border-color: #71717A;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background-color: #202024;
    color: #F4F4F5;
    border: 1px solid #3F3F46;
    selection-background-color: #3F3F46;
    selection-color: #FFFFFF;
}
QPushButton#SwapBtn {
    background-color: #27272A;
    color: #E4E4E7;
    border: 1px solid #3F3F46;
    border-radius: 6px;
    font-size: 14px;
    font-weight: 700;
}
QPushButton#SwapBtn:hover {
    background-color: #3F3F46;
    color: #FFFFFF;
}
QPushButton#SegBtn {
    background-color: #18181B;
    color: #A1A1AA;
    border: 1px solid #2E2E34;
    border-radius: 6px;
    padding: 7px 12px;
    font-size: 12.5px;
    font-weight: 600;
}
QPushButton#SegBtn:checked {
    background-color: #3F3F46;
    color: #FAFAFA;
    border: 1px solid #52525B;
}
QPushButton#SegBtn:hover:!checked {
    background-color: #27272A;
    color: #E4E4E7;
}
QPushButton#SecondaryBtn {
    background-color: #27272A;
    color: #E4E4E7;
    border: 1px solid #3F3F46;
    border-radius: 7px;
    padding: 8px 14px;
    font-size: 12.5px;
    font-weight: 600;
}
QPushButton#SecondaryBtn:hover {
    background-color: #3F3F46;
    color: #FFFFFF;
}
QPushButton#PrimaryBtn {
    background-color: #FAFAFA;
    color: #18181B;
    border: none;
    border-radius: 7px;
    padding: 8px 18px;
    font-size: 12.5px;
    font-weight: 700;
}
QPushButton#PrimaryBtn:hover {
    background-color: #E4E4E7;
}
QPushButton#TextBtn {
    background-color: transparent;
    color: #A1A1AA;
    border: none;
    padding: 6px 8px;
    font-size: 12px;
}
QPushButton#TextBtn:hover {
    color: #F4F4F5;
}
QCheckBox {
    color: #E4E4E7;
    font-size: 12.5px;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #52525B;
    background-color: #18181B;
}
QCheckBox::indicator:checked {
    background-color: #FAFAFA;
    border-color: #FAFAFA;
}
QSlider::groove:horizontal {
    height: 4px;
    background: #3F3F46;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #E4E4E7;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #FAFAFA;
    border: 1px solid #71717A;
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}
"""


class ToggleSwitch(QCheckBox):
    """체크(ON/OFF) 여부가 한눈에 명확히 구분되는 Zinc 다크 톤 토글 스위치."""

    def __init__(self, label: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(label, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(26)

    def hitButton(self, pos: QPoint) -> bool:
        return self.contentsRect().contains(pos)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        checked = self.isChecked()
        sw_w, sw_h = 40, 22
        sw_x = 0
        sw_y = (self.height() - sw_h) // 2

        # ON: 밝은 화이트 트랙(#FAFAFA) + 다크 노브(#18181B), OFF: 어두운 트랙(#27272A) + 회색 노브(#71717A)
        track_color = QColor("#FAFAFA") if checked else QColor("#27272A")
        border_color = QColor("#FAFAFA") if checked else QColor("#52525B")
        painter.setPen(QPen(border_color, 1))
        painter.setBrush(track_color)
        painter.drawRoundedRect(sw_x, sw_y, sw_w, sw_h, sw_h / 2, sw_h / 2)

        knob_d = 16
        knob_x = sw_x + sw_w - knob_d - 3 if checked else sw_x + 3
        knob_y = sw_y + (sw_h - knob_d) // 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#18181B") if checked else QColor("#71717A"))
        painter.drawEllipse(knob_x, knob_y, knob_d, knob_d)

        state_text = "ON" if checked else "OFF"
        state_color = QColor("#FAFAFA") if checked else QColor("#71717A")
        painter.setFont(QFont("Malgun Gothic", 9, QFont.Weight.Bold))
        painter.setPen(state_color)
        painter.drawText(
            QRect(sw_x + sw_w + 8, 0, 28, self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            state_text,
        )

        painter.setFont(QFont("Malgun Gothic", 10))
        painter.setPen(QColor("#F4F4F5") if checked else QColor("#A1A1AA"))
        painter.drawText(
            QRect(sw_x + sw_w + 40, 0, self.width() - (sw_x + sw_w + 42), self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self.text(),
        )


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        admin_tag = " (관리자)" if is_windows_admin() else ""
        self.setWindowTitle(f"TransOverlay{admin_tag}")
        self.setFixedWidth(480)

        self._app_icon = load_app_icon()
        self.setWindowIcon(self._app_icon)

        self._config = AppConfig.load()
        self._active_overlay: Optional[TranslatedOverlayWindow] = None
        self._region_selector: Optional[RegionSelectorOverlay] = None
        self._toast = LoadingToast()
        self._is_busy = False
        self._force_quit = False

        self._worker = FastGoogleTranslateWorker()
        self._worker.ready_changed.connect(self._on_ready_status)
        self._worker.status_changed.connect(self._on_worker_status)
        self._worker.translation_finished.connect(self._on_translation_finished)
        self._worker.translation_failed.connect(self._on_translation_failed)

        self._hotkeys = GlobalHotkeyManager(self)
        self._hotkeys.triggered_fullscreen.connect(self.capture_fullscreen)
        self._hotkeys.triggered_region.connect(self.capture_region)
        self._hotkeys.triggered_close.connect(self.close_overlay)
        self._hotkeys.hotkey_error.connect(self._on_hotkey_error)

        self._build_ui()
        self._setup_tray()
        self._apply_config_to_ui()
        self._hotkeys.register(self._config)
        threading.Thread(target=prewarm_google_connection, daemon=True).start()

    def _make_panel(self) -> tuple[QFrame, QVBoxLayout]:
        panel = QFrame()
        panel.setObjectName("Panel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)
        return panel, lay

    def _build_ui(self) -> None:
        self.setStyleSheet(CLEAN_NATIVE_STYLESHEET)

        central = QWidget()
        central.setObjectName("RootCanvas")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # 심플 헤더
        header = QHBoxLayout()
        header.setSpacing(10)
        logo = QLabel()
        logo.setFixedSize(34, 34)
        logo.setPixmap(self._app_icon.pixmap(34, 34))
        header.addWidget(logo)

        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        title = QLabel("TransOverlay")
        title.setObjectName("AppTitle")
        self._status_label = QLabel("초기화 중...")
        self._status_label.setObjectName("SubText")
        title_col.addWidget(title)
        title_col.addWidget(self._status_label)
        header.addLayout(title_col, 1)

        if sys.platform == "win32" and not is_windows_admin():
            btn_admin = QPushButton("관리자 실행")
            btn_admin.setObjectName("SecondaryBtn")
            btn_admin.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_admin.clicked.connect(self._relaunch_admin)
            header.addWidget(btn_admin)

        root.addLayout(header)

        # 1. 언어 및 캡처 방식 패널
        lang_panel, lang_lay = self._make_panel()

        lang_title = QLabel("번역 언어 (Google 번역)")
        lang_title.setObjectName("GroupLabel")
        lang_lay.addWidget(lang_title)

        lang_row = QHBoxLayout()
        lang_row.setSpacing(8)
        self._src_lang_combo = QComboBox()
        self._tgt_lang_combo = QComboBox()
        self._src_lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        self._tgt_lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        for label, code, _, _ in SUPPORTED_LANGUAGES:
            self._src_lang_combo.addItem(label, code)
            self._tgt_lang_combo.addItem(label, code)

        swap_btn = QPushButton("⇄")
        swap_btn.setObjectName("SwapBtn")
        swap_btn.setFixedSize(34, 32)
        swap_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        swap_btn.clicked.connect(self._swap_languages)

        lang_row.addWidget(self._src_lang_combo, 1)
        lang_row.addWidget(swap_btn)
        lang_row.addWidget(self._tgt_lang_combo, 1)
        lang_lay.addLayout(lang_row)


        root.addWidget(lang_panel)

        # 2. 단축키 패널
        hk_panel, hk_lay = self._make_panel()
        hk_title = QLabel("단축키")
        hk_title.setObjectName("GroupLabel")
        hk_lay.addWidget(hk_title)

        self._hk_fullscreen_edit = HotkeyRecorderEdit()
        self._hk_region_edit = HotkeyRecorderEdit()
        self._hk_close_edit = HotkeyRecorderEdit()
        for _ed in (self._hk_fullscreen_edit, self._hk_region_edit, self._hk_close_edit):
            _ed.hotkey_changed.connect(self._on_hotkeys_changed)
            _ed.recording_changed.connect(self._hotkeys.set_paused)

        for label_text, widget in [
            ("전체화면 캡처", self._hk_fullscreen_edit),
            ("영역 지정 캡처", self._hk_region_edit),
            ("오버레이 닫기", self._hk_close_edit),
        ]:
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setObjectName("FieldLabel")
            lbl.setFixedWidth(135)
            row.addWidget(lbl)
            row.addWidget(widget, 1)
            hk_lay.addLayout(row)
        root.addWidget(hk_panel)

        # 3. 동작 옵션 패널 (요청한 체크박스 딱 2개 + 투명도 슬라이더)
        opt_panel, opt_lay = self._make_panel()

        op_row = QHBoxLayout()
        op_lbl = QLabel("오버레이 투명도")
        op_lbl.setObjectName("FieldLabel")
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(20, 100)
        self._opacity_value_lbl = QLabel("100%")
        self._opacity_value_lbl.setStyleSheet("color: #E4E4E7; font-size: 12px; font-weight: 600;")
        self._opacity_value_lbl.setFixedWidth(38)
        self._opacity_value_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._opacity_slider.valueChanged.connect(self._on_opacity_slider_changed)
        op_row.addWidget(op_lbl)
        op_row.addWidget(self._opacity_slider, 1)
        op_row.addWidget(self._opacity_value_lbl)
        opt_lay.addLayout(op_row)
        self._click_close_chk = ToggleSwitch("클릭 시 오버레이 닫기")
        self._click_close_chk.toggled.connect(self._on_click_close_toggled)
        opt_lay.addWidget(self._click_close_chk)


        root.addWidget(opt_panel)

        # 하단 액션 바
        footer = QHBoxLayout()
        footer.setSpacing(8)

        btn_reset = QPushButton("초기화")
        btn_reset.setObjectName("TextBtn")
        btn_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_reset.clicked.connect(self._reset_defaults)

        btn_full = QPushButton("전체 캡처")
        btn_full.setObjectName("SecondaryBtn")
        btn_full.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_full.clicked.connect(self.capture_fullscreen)

        btn_region = QPushButton("영역 캡처")
        btn_region.setObjectName("SecondaryBtn")
        btn_region.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_region.clicked.connect(self.capture_region)

        footer.addWidget(btn_reset)
        footer.addStretch()
        footer.addWidget(btn_full)
        footer.addWidget(btn_region)
        root.addLayout(footer)

    def _on_click_close_toggled(self, checked: bool) -> None:
        if getattr(self, '_loading_ui', False):
            return
        self._config.close_on_mouse_click = bool(checked)
        self._config.save()
        self._status_label.setText('클릭 닫기 설정 적용됨')

    def _on_opacity_slider_changed(self, value: int) -> None:
        self._opacity_value_lbl.setText(f"{value}%")
        self._config.overlay_opacity = int(value)
        if self._active_overlay is not None:
            self._active_overlay.setWindowOpacity(value / 100.0)
        if not getattr(self, "_loading_ui", False):
            self._config.save()

    def _on_lang_changed(self) -> None:
        if getattr(self, "_loading_ui", False):
            return
        sl = str(self._src_lang_combo.currentData() or "en")
        tl = str(self._tgt_lang_combo.currentData() or "ko")
        if sl == tl:
            # 같은 언어를 고르면 이전 반대쪽 언어로 자동 스왑
            self._loading_ui = True
            prev_other = self._config.source_lang if sl == self._config.target_lang else self._config.target_lang
            if self.sender() is self._src_lang_combo:
                idx = self._tgt_lang_combo.findData(prev_other)
                if idx >= 0:
                    self._tgt_lang_combo.setCurrentIndex(idx)
                tl = str(self._tgt_lang_combo.currentData() or "ko")
            else:
                idx = self._src_lang_combo.findData(prev_other)
                if idx >= 0:
                    self._src_lang_combo.setCurrentIndex(idx)
                sl = str(self._src_lang_combo.currentData() or "en")
            self._loading_ui = False
        self._config.source_lang = sl
        self._config.target_lang = tl
        self._config.save()
        self._status_label.setText("언어 설정 자동 적용됨")

    def _on_hotkeys_changed(self) -> None:
        if getattr(self, "_loading_ui", False):
            return
        hotkeys = [
            self._hk_fullscreen_edit.hotkey,
            self._hk_region_edit.hotkey,
            self._hk_close_edit.hotkey,
        ]
        non_empty = [h for h in hotkeys if h]
        if len(non_empty) != len(set(non_empty)):
            self._toast.show_notice("중복된 단축키는 사용할 수 없습니다.", duration_ms=2500)
            self._apply_config_to_ui()
            return
        self._config.hotkey_fullscreen = self._hk_fullscreen_edit.hotkey
        self._config.hotkey_region = self._hk_region_edit.hotkey
        self._config.hotkey_close_overlay = self._hk_close_edit.hotkey
        self._config.save()
        self._hotkeys.set_paused(False)
        if self._hotkeys.register(self._config):
            self._status_label.setText("단축키 자동 적용됨")

    def _swap_languages(self) -> None:
        self._loading_ui = True
        src_idx = self._src_lang_combo.currentIndex()
        tgt_idx = self._tgt_lang_combo.currentIndex()
        self._src_lang_combo.setCurrentIndex(tgt_idx)
        self._tgt_lang_combo.setCurrentIndex(src_idx)
        self._loading_ui = False
        self._on_lang_changed()

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(self._app_icon)
        self._tray.setToolTip("TransOverlay")

        menu = QMenu()
        act_full = QAction("전체화면 캡처 번역", self)
        act_full.triggered.connect(self.capture_fullscreen)
        act_region = QAction("영역 지정 캡처 번역", self)
        act_region.triggered.connect(self.capture_region)
        act_close_ov = QAction("오버레이 닫기", self)
        act_close_ov.triggered.connect(self.close_overlay)
        act_show = QAction("설정 열기", self)
        act_show.triggered.connect(self._show_from_tray)
        act_quit = QAction("종료", self)
        act_quit.triggered.connect(self._quit_app)

        menu.addAction(act_full)
        menu.addAction(act_region)
        menu.addAction(act_close_ov)
        menu.addSeparator()
        menu.addAction(act_show)
        menu.addAction(act_quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _apply_config_to_ui(self) -> None:
        self._loading_ui = True
        s_idx = self._src_lang_combo.findData(self._config.source_lang)
        t_idx = self._tgt_lang_combo.findData(self._config.target_lang)
        self._src_lang_combo.setCurrentIndex(max(0, s_idx))
        self._tgt_lang_combo.setCurrentIndex(max(0, t_idx))

        self._hk_fullscreen_edit.set_hotkey(self._config.hotkey_fullscreen)
        self._hk_region_edit.set_hotkey(self._config.hotkey_region)
        self._hk_close_edit.set_hotkey(self._config.hotkey_close_overlay)
        self._opacity_slider.setValue(self._config.overlay_opacity)
        self._opacity_value_lbl.setText(f"{self._config.overlay_opacity}%")
        self._click_close_chk.setChecked(self._config.close_on_mouse_click)
        self._loading_ui = False

    def _save_settings(self) -> None:
        sl = str(self._src_lang_combo.currentData() or "en")
        tl = str(self._tgt_lang_combo.currentData() or "ko")
        if sl == tl:
            QMessageBox.warning(self, "언어 설정", "원본 언어와 번역 언어를 다르게 선택해 주세요.")
            return

        hotkeys = [
            self._hk_fullscreen_edit.hotkey,
            self._hk_region_edit.hotkey,
            self._hk_close_edit.hotkey,
        ]
        non_empty = [h for h in hotkeys if h]
        if len(non_empty) != len(set(non_empty)):
            QMessageBox.warning(self, "단축키 중복", "서로 다른 기능에 동일한 단축키가 지정되었습니다.")
            return

        self._config.source_lang = sl
        self._config.target_lang = tl
        self._config.hotkey_fullscreen = self._hk_fullscreen_edit.hotkey
        self._config.hotkey_region = self._hk_region_edit.hotkey
        self._config.hotkey_close_overlay = self._hk_close_edit.hotkey
        self._config.overlay_opacity = self._opacity_slider.value()
        self._config.turbo_mode = True
        self._config.close_on_mouse_click = self._click_close_chk.isChecked()
        self._config.save()

        if self._hotkeys.register(self._config):
            self._status_label.setText("설정이 적용되었습니다")

    def _reset_defaults(self) -> None:
        self._config = AppConfig()
        self._apply_config_to_ui()
        self._save_settings()

    def _relaunch_admin(self) -> None:
        if relaunch_as_admin():
            self._quit_app()
        else:
            QMessageBox.warning(self, "권한 상승 취소", "관리자 권한 실행이 취소되었습니다.")

    def _grab_active_screen(self) -> tuple[QPixmap, QRect]:
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            raise RuntimeError("캡처 가능한 모니터를 찾을 수 없습니다.")
        return screen.grabWindow(0), screen.geometry()

    def _clear_transient_windows(self) -> bool:
        had_any = False
        if self._region_selector is not None:
            try:
                self._region_selector.close()
            except Exception:
                pass
            self._region_selector = None
            had_any = True
        if self._toast.isVisible():
            self._toast.hide()
            had_any = True
        if self._active_overlay is not None:
            self.close_overlay()
            had_any = True
        return had_any

    def capture_fullscreen(self) -> None:
        if self._is_busy:
            self._toast.show_message("번역 처리 중...")
            return

        if self._clear_transient_windows():
            QApplication.processEvents()
            time.sleep(0.02)

        try:
            pixmap, screen_geo = self._grab_active_screen()
        except Exception as exc:
            self._on_translation_failed(str(exc))
            return

        self._dispatch_translation(pixmap, screen_geo)

    def capture_region(self) -> None:
        if self._is_busy:
            self._toast.show_message("번역 처리 중...")
            return

        if self._clear_transient_windows():
            QApplication.processEvents()
            time.sleep(0.02)

        try:
            pixmap, screen_geo = self._grab_active_screen()
        except Exception as exc:
            self._on_translation_failed(str(exc))
            return

        self._region_selector = RegionSelectorOverlay(pixmap, screen_geo)
        self._region_selector.region_selected.connect(self._dispatch_translation)
        self._region_selector.selection_cancelled.connect(
            lambda: self._status_label.setText("영역 캡처 취소됨")
        )
        self._region_selector.show()
        self._region_selector.activateWindow()

    def _dispatch_translation(self, pixmap: QPixmap, target_rect: QRect) -> None:
        try:
            orig_img = qpixmap_to_pil(pixmap)
        except Exception as exc:
            self._on_translation_failed(f"캡처 이미지 변환 실패: {exc}")
            return

        self._is_busy = True
        self._toast.show_message("번역 중...")
        self._status_label.setText("번역 처리 중...")
        self._worker.submit(orig_img, target_rect, self._config)

    def _on_ready_status(self, msg: str) -> None:
        if not self._is_busy:
            self._status_label.setText(msg)

    def _on_worker_status(self, msg: str) -> None:
        self._status_label.setText(msg)
        if self._is_busy:
            self._toast.show_message(msg)

    def _on_translation_finished(
        self, original_img: Image.Image, translated_img: Image.Image, target_rect: QRect, elapsed: float
    ) -> None:
        self._is_busy = False
        self._toast.hide()
        self._status_label.setText(f"완료 ({elapsed:.2f}초)")

        self.close_overlay()
        self._active_overlay = TranslatedOverlayWindow(
            original_img=original_img,
            translated_img=translated_img,
            target_rect=target_rect,
            opacity_percent=self._config.overlay_opacity,
            close_on_click=self._config.close_on_mouse_click,
        )
        self._active_overlay.show()
        apply_game_topmost_window(int(self._active_overlay.winId()))

    def _on_translation_failed(self, error_msg: str) -> None:
        self._is_busy = False
        short_msg = error_msg.strip().splitlines()[-1] if error_msg.strip() else "번역 실패"
        self._status_label.setText(short_msg)
        self._toast.show_notice(short_msg, duration_ms=2800)

    def _on_hotkey_error(self, error_msg: str) -> None:
        self._status_label.setText(error_msg)
        self._toast.show_notice(error_msg, duration_ms=3000)

    def close_overlay(self) -> None:
        if self._active_overlay is not None:
            try:
                self._active_overlay.close()
            except Exception:
                pass
            self._active_overlay = None

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_from_tray()

    def closeEvent(self, event) -> None:
        if not self._force_quit and self._tray.isVisible():
            self.hide()
            self._tray.showMessage(
                "TransOverlay",
                "트레이에서 계속 실행 중입니다.",
                QSystemTrayIcon.MessageIcon.Information,
                1800,
            )
            event.ignore()
            return

        self.close_overlay()
        self._toast.close()
        self._hotkeys.stop()
        self._worker.shutdown()
        self._tray.hide()
        event.accept()

    def _quit_app(self) -> None:
        self._force_quit = True
        self.close()
        QApplication.instance().quit()


def main() -> None:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(load_app_icon())

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
