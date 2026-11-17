"""PNG 像素解码/编码（纯标准库）：让像素级合规校验在**没有 Pillow** 时也能跑。

只覆盖自己产出的 PNG 子集（8 位 RGB/RGBA、非隔行），足够做白底纯度/主体占比/留白这类
统计；JPEG/WebP 仍走 Pillow（可选依赖），没有就退化为"只看头部尺寸 + 文件大小"。
"""

from __future__ import annotations

import struct
import zlib

from app.agents_image.png import encode_png

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class PngDecodeError(ValueError):
    """不是我们支持解码的 PNG（隔行/16 位/调色板等）。"""


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> bytearray:
    stride = width * channels
    out = bytearray(stride * height)
    prev = bytearray(stride)
    pos = 0
    for row in range(height):
        if pos >= len(raw):
            raise PngDecodeError("PNG 数据不完整")
        filter_type = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        if len(line) != stride:
            raise PngDecodeError("PNG 行长度不匹配")
        pos += stride
        if filter_type == 0:
            pass
        elif filter_type == 1:                     # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:                     # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filter_type == 3:                     # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:                     # Paeth
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up_left = prev[i - channels] if i >= channels else 0
                p = left + prev[i] - up_left
                pa, pb, pc = abs(p - left), abs(p - prev[i]), abs(p - up_left)
                if pa <= pb and pa <= pc:
                    pred = left
                elif pb <= pc:
                    pred = prev[i]
                else:
                    pred = up_left
                line[i] = (line[i] + pred) & 0xFF
        else:
            raise PngDecodeError(f"不支持的 PNG 行过滤类型：{filter_type}")
        out[row * stride:(row + 1) * stride] = line
        prev = line
    return out


def decode_png(data: bytes) -> tuple[int, int, int, bytearray]:
    """返回 (width, height, channels, pixels)；像素按行紧密排列。"""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PngDecodeError("不是 PNG")
    pos = 8
    width = height = bit_depth = color_type = interlace = 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    channels = _CHANNELS.get(color_type)
    if channels is None or bit_depth != 8 or interlace != 0:
        raise PngDecodeError(
            f"只支持 8 位非隔行 PNG（收到 bit_depth={bit_depth} color_type={color_type} "
            f"interlace={interlace}）")
    raw = zlib.decompress(bytes(idat))
    pixels = _unfilter(raw, width, height, channels)
    return width, height, channels, pixels


def rgb_stats(data: bytes, *, white_threshold: int = 250) -> dict:
    """统计白底纯度与主体占比（对 RGBA 取前三通道，忽略 alpha）。"""
    width, height, channels, pixels = decode_png(data)
    total = width * height
    white = 0
    subject = 0
    for index in range(total):
        base = index * channels
        r, g, b = pixels[base], pixels[base + 1], pixels[base + 2]
        if r >= white_threshold and g >= white_threshold and b >= white_threshold:
            white += 1
        elif max(r, g, b) - min(r, g, b) > 12 or (r + g + b) < 700:
            subject += 1
    return {
        "width": width, "height": height,
        "white_ratio": round(white / total, 4) if total else 0.0,
        "subject_ratio": round(subject / total, 4) if total else 0.0,
        "margin_ratio": round(_margin_ratio(width, height, channels, pixels,
                                            white_threshold), 4),
    }


def _margin_ratio(width: int, height: int, channels: int, pixels: bytearray,
                  threshold: int) -> float:
    """四周留白比例：边框一圈里"接近白"的像素占比（越低说明商品顶到边）。"""
    border = max(1, int(min(width, height) * 0.04))
    total = 0
    white = 0
    for y in range(height):
        for x in range(width):
            if border <= x < width - border and border <= y < height - border:
                continue
            base = (y * width + x) * channels
            r, g, b = pixels[base], pixels[base + 1], pixels[base + 2]
            total += 1
            if r >= threshold and g >= threshold and b >= threshold:
                white += 1
    return white / total if total else 0.0


def whiten_near_white(width: int, height: int, channels: int, pixels: bytearray,
                      *, threshold: int = 250) -> bytes:
    """把接近白的像素归白（`--fix`）：只改像素值，不改尺寸。"""
    out = bytearray(pixels)
    for index in range(width * height):
        base = index * channels
        if out[base] >= threshold and out[base + 1] >= threshold and out[base + 2] >= threshold:
            out[base] = out[base + 1] = out[base + 2] = 255
    rgb = bytearray()
    for index in range(width * height):
        base = index * channels
        rgb += bytes((out[base], out[base + 1], out[base + 2]))
    return encode_png(width, height, bytes(rgb))


def crop_to_square(width: int, height: int, channels: int, pixels: bytearray) -> bytes:
    """居中裁成正方形（`--fix`：非 1:1 时用）。"""
    side = min(width, height)
    offset_x = (width - side) // 2
    offset_y = (height - side) // 2
    rgb = bytearray()
    for y in range(side):
        for x in range(side):
            base = ((y + offset_y) * width + (x + offset_x)) * channels
            rgb += bytes((pixels[base], pixels[base + 1], pixels[base + 2]))
    return encode_png(side, side, bytes(rgb))


def resize(width: int, height: int, channels: int, pixels: bytearray,
           new_width: int, new_height: int) -> bytes:
    """最近邻缩放（`--fix`：长边不足平台下限时放大小图；不追求画质）。"""
    rgb = bytearray()
    for y in range(new_height):
        source_y = min(height - 1, int(y * height / new_height))
        for x in range(new_width):
            source_x = min(width - 1, int(x * width / new_width))
            base = (source_y * width + source_x) * channels
            rgb += bytes((pixels[base], pixels[base + 1], pixels[base + 2]))
    return encode_png(new_width, new_height, bytes(rgb))


__all__ = ["PngDecodeError", "decode_png", "rgb_stats", "whiten_near_white",
           "crop_to_square", "resize"]
