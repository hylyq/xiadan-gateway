"""虚拟键码映射表"""

# Windows 虚拟键码（VK_*）
KEY_MAP = {
    # 功能键
    "F1": 0x70,
    "F2": 0x71,
    "F3": 0x72,
    "F4": 0x73,
    "F5": 0x74,
    "F6": 0x75,
    "F7": 0x76,
    "F8": 0x77,
    "F9": 0x78,
    "F10": 0x79,
    "F11": 0x7A,
    "F12": 0x7B,

    # 修饰键
    "CTRL": 0x11,
    "SHIFT": 0x10,
    "ALT": 0x12,

    # 编辑键
    "ENTER": 0x0D,
    "ESC": 0x1B,
    "TAB": 0x09,
    "BACKSPACE": 0x08,
    "DELETE": 0x2E,
    "INSERT": 0x2D,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,

    # 方向键
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,

    # 空格
    "SPACE": 0x20,
}
