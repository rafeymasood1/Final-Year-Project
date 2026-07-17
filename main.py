import win32gui, win32process, win32con, win32api, win32ui
import psutil
import uiautomation as auto
from win32com.shell import shell, shellcon
from PIL import Image
import os

import cv2
import numpy as np

import dxcam




OUTPUT_DIR = "icons"
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ------------------------------------------------------------------
# Icon extraction helpers
# ------------------------------------------------------------------

def hicon_to_image(hicon, size=32):
    hdc = win32ui.CreateDCFromHandle(win32gui.GetDC(0))
    hbmp = win32ui.CreateBitmap()
    hbmp.CreateCompatibleBitmap(hdc, size, size)
    hdc_mem = hdc.CreateCompatibleDC()
    hdc_mem.SelectObject(hbmp)
    hdc_mem.DrawIcon((0, 0), hicon)

    bmpinfo = hbmp.GetInfo()
    bmpstr = hbmp.GetBitmapBits(True)
    img = Image.frombuffer(
        "RGBA",
        (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
        bmpstr, "raw", "BGRA", 0, 1
    )

    win32gui.DestroyIcon(hicon)
    hdc_mem.DeleteDC()
    hdc.DeleteDC()
    win32gui.DeleteObject(hbmp.GetHandle())

    return img


def get_window_icon(hwnd):
    hicon = win32gui.SendMessage(hwnd, win32con.WM_GETICON, win32con.ICON_BIG, 0)
    if not hicon:
        hicon = win32gui.SendMessage(hwnd, win32con.WM_GETICON, win32con.ICON_SMALL, 0)
    if not hicon:
        try:
            hicon = win32gui.GetClassLong(hwnd, win32con.GCL_HICON)
        except Exception:
            hicon = 0
    if not hicon:
        return None
    try:
        return hicon_to_image(hicon)
    except Exception:
        return None


def get_file_icon(path, large=True):
    if not path or not os.path.exists(path):
        return None
    flags = shellcon.SHGFI_ICON | (shellcon.SHGFI_LARGEICON if large else shellcon.SHGFI_SMALLICON)
    try:
        ret, info = shell.SHGetFileInfo(path, 0, flags)
        hicon = info[0]
        if not hicon:
            return None
        return hicon_to_image(hicon)
    except Exception:
        return None


def save_icon(img, name):
    if img is None:
        return None
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    path = os.path.join(OUTPUT_DIR, f"{safe_name}.png")
    try:
        img.save(path)
        return path
    except Exception:
        return None


# ------------------------------------------------------------------
# Active window info
# ------------------------------------------------------------------
import time
print("Switch to the target window... capturing in 3 seconds")
time.sleep(3)

hwnd = win32gui.GetForegroundWindow()
hwnd = win32gui.GetForegroundWindow()
title = win32gui.GetWindowText(hwnd)
class_name = win32gui.GetClassName(hwnd)
left, top, right, bottom = win32gui.GetWindowRect(hwnd)
width = right - left
height = bottom - top

TID, PID = win32process.GetWindowThreadProcessId(hwnd)
process = psutil.Process(PID)

window_info = {
    "hwnd": hwnd,
    "title": title,
    "class": class_name,
    "pid": PID,
    "tid": TID,
    "rect": (left, top, right, bottom),
    "width": width,
    "height": height,
    "process_name": process.name(),
    "exe": process.exe(),
}

for key, value in window_info.items():
    print(f"{key:15}: {value}")




# ------------------------------------------------------------------
# UI tree walk (with per-control icon extraction where possible)
# ------------------------------------------------------------------

print("\n===== UI TREE =====\n")

boxes = []


def walk(control, depth=0):
    indent = "    " * depth

    rect = control.BoundingRectangle

    print(
        f"{indent}{control.ControlTypeName:<20} "
        f"Name='{control.Name}' "
        f"Class='{control.ClassName}' "
        f"Type={control.ControlType} "
        f"Enabled={control.IsEnabled} "
        f"Offscreen={control.IsOffscreen} "
        f"Focused={control.HasKeyboardFocus} "
        f"Rect={rect}"
    )

    # Save visible rectangles
    if not control.IsOffscreen and rect and rect.width() > 1 and rect.height() > 1:
        boxes.append(
            (
                int(rect.left), int(rect.top), int(rect.right), int(rect.bottom), control.ControlTypeName, control.Name,
            )
        )

    # if control.NativeWindowHandle:
    #     icon_img = get_window_icon(control.NativeWindowHandle)
    #     if icon_img:
    #         saved_path = save_icon(
    #             icon_img,
    #             f"ctrl_{control.NativeWindowHandle}_{control.ControlTypeName}",
    #         )
    #         print(f"{indent}  Icon Saved : {saved_path}")

    print()

    for child in control.GetChildren():
        walk(child, depth + 1)


root = auto.ControlFromHandle(hwnd)
walk(root)
print(f"\nTotal visible elements: {len(boxes)}")
# -------------------------------------------------------
# Draw bounding boxes
# -------------------------------------------------------
#
# screen_w = win32api.GetSystemMetrics(0)
# screen_h = win32api.GetSystemMetrics(1)
#
# canvas = np.zeros((screen_h, screen_w, 3), dtype=np.uint8)
#
# for left, top, right, bottom, ctype, name in boxes:
#
#     cv2.rectangle(
#         canvas,
#         (left, top),
#         (right, bottom),
#         (0, 255, 0),
#         2,
#     )
#
#     label = ctype
#     if name:
#         label += ": " + name[:20]
#
#     cv2.putText(
#         canvas,
#         label,
#         (left, max(top - 5, 15)),
#         cv2.FONT_HERSHEY_SIMPLEX,
#         0.45,
#         (0, 255, 0),
#         1,
#         cv2.LINE_AA,
#     )
#
# cv2.imshow("UI Bounding Boxes", canvas)
# cv2.waitKey(0)
# cv2.destroyAllWindows()

### DX CAM

camera = dxcam.create()
frame = camera.grab()
canvas = frame.copy()

for left, top, right, bottom, ctype, name in boxes:
    cv2.rectangle(canvas, (left, top), (right, bottom), (0,255,0), 2)

cv2.imshow("Overlay", canvas)
cv2.waitKey(0)

print(len(boxes))