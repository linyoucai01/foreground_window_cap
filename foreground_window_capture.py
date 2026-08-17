# cython: language_level=3, annotation_typing=False
# -*- coding: utf-8 -*-
import ctypes
from ctypes import wintypes
from datetime import datetime
from typing import ClassVar
from configparser import ConfigParser
from logging import handlers
import mss
import mss.tools
import dataclasses
import io
import json
import logging
import os
import socket,requests
import struct
import threading
import time
import uuid
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypedDict

from PIL import Image, UnidentifiedImageError

MODULE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = (
    MODULE_DIR.parent
    if MODULE_DIR.name.casefold() == "runtime"
    else MODULE_DIR
)
CONFIG_PATH = RUNTIME_DIR / "config.json"
LOG_DIR = RUNTIME_DIR / "logs"
STATE_DIR = RUNTIME_DIR / "state"
class Logger():
    level_relations = {
        'debug': logging.DEBUG,
        0: logging.DEBUG,
        'info': logging.INFO,
        1: logging.INFO,
        'warning': logging.WARNING,
        2: logging.WARNING,
        'error': logging.ERROR,
        3: logging.ERROR,
        'crit': logging.CRITICAL,
        4: logging.CRITICAL
    }  # 日志级别关系映射

    def __init__(self, pp=9222, level=2, when='H', backCount=10000,
                 # fmt='%(asctime)s - %(pathname)s[line:%(lineno)d] - %(levelname)s: %(message)s'):
                 fmt='[%(asctime)s %(filename)s:%(funcName)s:%(lineno)d %(levelname)s] %(message)s', print_f=True):
        # options.log_to_stderr=False

        # root_path = os.path.dirname(os.path.realpath(__file__))
        # filename_l = os.path.join(root_path,init_cfg.log_path)
        # filename = os.path.join(filename_l,f'{pp}.log')
        # try:
        #     if not os.path.exists(os.path.join(root_path, 'log2')):
        #         os.mkdir('log2')
        # except Exception as e:
        #     print(f'make dir log2 error={e}')
        log_p = str(LOG_DIR)
        if not os.path.exists(log_p):
            os.mkdir(log_p)
        filename = os.path.join(log_p, f'{pp}.log')

        self.logger = logging.getLogger(filename)
        format_str = logging.Formatter(fmt)  # 设置日志格式
        self.logger.setLevel(self.level_relations.get(level))  # 设置日志级别
        if print_f:
            sh = logging.StreamHandler()  # 往屏幕上输出
            sh.setFormatter(format_str)  # 设置屏幕上显示的格式
            self.logger.addHandler(sh)
        # th = handlers.TimedRotatingFileHandler(filename=filename, when=when, backupCount=backCount,encoding='utf-8')  # 往文件里写入#指定间隔时间自动生成文件的处理器
        th = handlers.RotatingFileHandler(filename=filename, maxBytes=31457280, backupCount=backCount,
                                          encoding='utf-8')  # 往文件里写入#指定间隔时间自动生成文件的处理器

        # 实例化TimedRotatingFileHandler
        # interval是时间间隔，backupCount是备份文件的个数，如果超过这个个数，就会自动删除，when是间隔的时间单位，单位有以下几种：
        # S 秒
        # M 分
        # H 小时、
        # D 天、
        # W 每星期（interval==0时代表星期一）
        # midnight 每天凌晨
        th.setFormatter(format_str)  # 设置文件里写入的格式
        # 把对象加到logger里
        self.logger.addHandler(th)

    @property
    def ilog(self):
        return self.logger.info

    @property
    def elog(self):
        return self.logger.error

    @property
    def wlog(self):
        return self.logger.warning

    @property
    def log(self, flag=0):
        if flag == 0:
            return self.logger.debug
        elif flag == 1:
            return self.logger.info
        elif flag == 2:
            return self.logger.warning
        elif flag == 3:
            return self.logger.error
        return self.logger.info
l = Logger(pp="agent", level=1, print_f=True)
# TCP和内存保护。
MAX_CONNECTIONS = 512
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 100_000_000
MAX_PLUG_INFO_BYTES = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 10.0
SOCKET_IO_TIMEOUT_SECONDS = 30.0
STATUS_OK = 0
CLIENT_ID_FILE = STATE_DIR / "client_id"
# 必须与服务端的 SEND_ACK 保持一致。
WAIT_FOR_ACK = False
ACK_TIMEOUT_SECONDS = 130.0
# =============================================================================
# 二、协议常量：必须与 image_tcp_ocr_server.py 保持一致
# =============================================================================

PROTOCOL_VERSION = 2
MAGIC_IMAGE = b"PIMG"
MAGIC_ACK = b"PACK"

MESSAGE_TYPE_IMAGE = 1
MESSAGE_TYPE_ACK = 2

CODEC_WEBP = 1
CODEC_JPEG = 2
CODEC_PNG = 3
CODEC_AVIF = 4

# flags 的 bit0 表示无损图片，bit1 表示图片包含 Alpha 通道。
FLAG_LOSSLESS = 1 << 0
FLAG_HAS_ALPHA = 1 << 1

# 8 字节 body 长度，不包括长度字段自身。
LENGTH_PREFIX = struct.Struct("!Q")
# 协议第2版固定头，共56字节：
# magic/version/type/codec/flags/client_uuid/message_id/time/width/height/
# plug_info_json_length/crc32
IMAGE_HEADER = struct.Struct("!4sBBBB16sQQIIII")
# 服务端可选 ACK 的固定结构。
ACK_BODY = struct.Struct("!4sBBBB16sQQII")
assert LENGTH_PREFIX.size == 8
assert IMAGE_HEADER.size == 56
assert ACK_BODY.size == 48
def load_or_create_client_id()->uuid.UUID:
    try:
        return uuid.UUID(CLIENT_ID_FILE.read_text(encoding="ascii").strip())
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"客户端 UUID 文件无效：{CLIENT_ID_FILE}") from exc
    new_client_id = uuid.uuid4()
    CLIENT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)

    try:
        # 使用 x 模式防止同一时刻启动的两个进程互相覆盖 UUID。
        with CLIENT_ID_FILE.open("x", encoding="ascii") as file:
            file.write(f"{new_client_id}\n")
            file.flush()
            os.fsync(file.fileno())
        return new_client_id
    except FileExistsError:
        # 另一个进程已经先创建成功，读取它创建的 UUID。
        return uuid.UUID(CLIENT_ID_FILE.read_text(encoding="ascii").strip())
@dataclasses.dataclass(slots=True, frozen=True)
class EncodedImageInfo:
    """从已编码图片头部读取出的协议元数据。"""

    codec: int
    width: int
    height: int
    flags: int
    format_name: str


@dataclasses.dataclass(slots=True, frozen=True)
class AckResult:
    """仅在客户端和服务端都开启 ACK 时返回。"""

    status: int
    message_id: int
    server_timestamp_ms: int
    detail_code: int
    received_bytes: int

    @property
    def succeeded(self) -> bool:
        """服务端是否成功完成 OCR 处理。"""

        return self.status == STATUS_OK


class ImageProtocolError(RuntimeError):
    """图片格式、协议字段或 ACK 不符合约定。"""

def encode_file_as_lossless_webp(image_path: str | Path) -> bytes:
    """
    把磁盘图片编码成适合 OCR 传输的无损 WEBP 字节。

    如果截图程序已经通过 Pillow/OpenCV 生成了 WEBP 字节，就不需要调用本函数。
    ``method=4`` 在压缩速度与图片体积之间比较均衡；客户端 CPU 很充足时可改为 6。
    """

    source_path = Path(image_path)
    with Image.open(source_path) as source_image:
        # 保留透明通道；没有透明通道时统一转换成 RGB。
        target_mode = "RGBA" if "A" in source_image.getbands() else "RGB"
        converted_image = source_image.convert(target_mode)

        output = io.BytesIO()
        converted_image.save(
            output,
            format="WEBP",
            lossless=True,
            quality=100,
            method=4,
            exact=True,
        )
        return output.getvalue()


def _webp_is_lossless(image_bytes: bytes) -> bool:
    """遍历 WEBP 的 RIFF chunk，判断图像数据是否使用 VP8L 无损编码。"""

    offset = 12
    byte_count = len(image_bytes)

    while offset + 8 <= byte_count:
        chunk_type = image_bytes[offset : offset + 4]
        chunk_size = int.from_bytes(
            image_bytes[offset + 4 : offset + 8],
            byteorder="little",
        )

        if chunk_type == b"VP8L":
            return True
        if chunk_type == b"VP8 ":
            return False

        # RIFF chunk 长度不足偶数时会附加一个 padding 字节。
        offset += 8 + chunk_size + (chunk_size & 1)

    return False


def inspect_encoded_image(image_bytes: bytes) -> EncodedImageInfo:
    """验证图片文件字节，并读取编码、宽高、无损和 Alpha 信息。"""

    if not image_bytes:
        raise ValueError("imgbyte 不能为空")

    try:
        # Pillow 在这里主要读取图片头，不会主动把整张图片解码成像素矩阵。
        with Image.open(io.BytesIO(image_bytes)) as image:
            format_name = (image.format or "").upper()
            width, height = image.size
            has_alpha = "A" in image.getbands() or "transparency" in image.info
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(
            "imgbyte 不是有效的 WEBP/JPEG/PNG/AVIF 图片文件字节；"
            "如果当前数据是 BGR/RGB 原始像素，请先编码图片"
        ) from exc

    codec_by_format = {
        "WEBP": CODEC_WEBP,
        "JPEG": CODEC_JPEG,
        "JPG": CODEC_JPEG,
        "PNG": CODEC_PNG,
        "AVIF": CODEC_AVIF,
    }

    try:
        codec = codec_by_format[format_name]
    except KeyError as exc:
        raise ValueError(
            f"服务端不支持 {format_name or '未知'} 图片，只支持 WEBP/JPEG/PNG/AVIF"
        ) from exc

    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError(f"图片尺寸非法或过大：{width}x{height}")

    is_lossless = format_name == "PNG" or (
        format_name == "WEBP" and _webp_is_lossless(image_bytes)
    )

    flags = 0
    if is_lossless:
        flags |= FLAG_LOSSLESS
    if has_alpha:
        flags |= FLAG_HAS_ALPHA

    return EncodedImageInfo(
        codec=codec,
        width=width,
        height=height,
        flags=flags,
        format_name=format_name,
    )

class Const:
    HOST: ClassVar[str]
    PORT: ClassVar[int]
    apppassword: ClassVar[str]
    short_id: ClassVar[str]
    display_name: ClassVar[str]
    remark: ClassVar[str]
    # MYSQL_PORT: ClassVar[int]

    @classmethod
    def load(cls, conf_path: Path = CONFIG_PATH) -> None:
        try:
            raw = json.loads(conf_path.read_text(encoding="utf-8"))
            network = raw["network"]
            device = raw["device"]
            capture = raw["capture"]
        except (KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取运行配置 {conf_path}: {exc}") from exc

        cls.HOST = str(network["host"])
        cls.PORT = int(network["port"])
        cls.interel = float(capture["interval_seconds"])
        cls.apppassword = str(device["app_password"])
        cls.display_name = str(device["display_name"])
        cls.short_id = str(device["short_id"])
        cls.remark = str(device["remark"])
        cls.root_url = str(network["root_url"]).rstrip("/")
        cls.verify_tls = bool(network.get("verify_tls", True))
        cls.request_timeout_seconds = float(network.get("request_timeout_seconds", 15))
        cls.save_capture_images = bool(capture.get("save_capture_images", False))
Const.load()
device_id=load_or_create_client_id()
headers = {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9",
        # "authorization": "Bearer N5hUlpnjD8D3kk0DT9AGwPVdInUAJmnOgrPynk8rHNk",
        "content-type": "application/json",
        "user-agent": "Android superpower",
        # "referer": "http://43.167.236.32:82/",
    }
rooturl = Const.root_url
TEST_PLUG_INFO:Mapping[str, Any] = {'device_id':str(device_id),'device_name':Const.display_name,'remark':Const.remark,'device_short_id':Const.short_id}
def phone_verify_password():
    '''app打开的时候就要输入密码 并进行验证， 请求头里的
    下面是 body 字段的组合方式
    display_name是 设备名称/市场名称
    short_id：用户自定义的手机名 拿不到就在app里 弄一个输入框，空的话8位随机数，甲方拿去定义 为 小孙的工作手机   在后台就能知道是谁的手机
    model 是这台手机的唯一标识， 传给我服务器需要 存数据库 作为 一台手机的唯一标识 这个标识要存在内存，后面两个 接口要传这个值 我才知道是谁发给服务器的 '''
    uri = '/verify_device'
    url=f'{rooturl}{uri}'

    body = {'password': Const.apppassword,'display_name':Const.display_name,'short_id':Const.short_id,'remark':Const.remark,'model':str(device_id)}
    # body = {'password': '886262','display_name':'HONOR MTN-AN00','short_id':'17a4ef41','remark':'战狼','model':'2a203810-554a5d-eweds4-we52d5'}
    response = requests.request(
        "POST", url=url, verify=Const.verify_tls, headers=headers,
        data=json.dumps(body), timeout=Const.request_timeout_seconds,
    )

    respon_json=json.loads(response.text)
    return respon_json

def device_heart():
    '''device_id 是 phone_verify_password 接口 body里的 model值.你存在app里 '''
    try:
        url=f'{rooturl}/health?device_id={str(device_id)}'
        requests.get(
            url, verify=Const.verify_tls, headers=headers,
            timeout=Const.request_timeout_seconds,
        )

    except Exception as e:
        l.ilog(f'心跳出错了 ={e}')
    time.sleep(10)

def loop_verify():
    while True:
        try:
            device_heart()
            phone_verify_password()
        except Exception as e:
            l.ilog(f'验证失败 {e}')
        time.sleep(300)

class ImageTcpClient:
    """
    可复用的 TCP 图片客户端。

    一个实例持有一个 TCP 长连接。``send_dict()`` 使用线程锁保护完整数据帧，
    即使截图回调来自不同线程，也不会把两张图片的包头和图片字节交叉发送。
    """

    def __init__(self) -> None:
        """
        根据文件顶部常量初始化。

        构造函数不接收任何配置参数，防止运行时传入的地址、端口或 ACK 设置
        与服务端约定不一致。需要修改时直接改“全部硬编码参数”区域。
        """

        self.host = Const.HOST
        self.port = Const.PORT
        self.client_id = device_id
        self.wait_for_ack = WAIT_FOR_ACK
        self.connect_timeout = CONNECT_TIMEOUT_SECONDS
        self.socket_io_timeout = SOCKET_IO_TIMEOUT_SECONDS
        self.ack_timeout = ACK_TIMEOUT_SECONDS

        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self) -> None:
        """建立 TCP 长连接；重复调用不会创建第二条连接。"""

        with self._send_lock:
            self._connect_unlocked()

    def _connect_unlocked(self) -> None:
        if self._socket is not None:
            return

        client_socket = socket.create_connection(
            (self.host, self.port),
            timeout=self.connect_timeout,
        )

        # 图片包不需要 Nagle 合并等待，降低小包头的发送延迟。
        client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        client_socket.settimeout(self.socket_io_timeout)

        self._socket = client_socket
        l.ilog(
            f"已连接图片服务器 { self.host} {self.port}  {self.client_id}",
        )

    def close(self) -> None:
        """关闭长连接；关闭后下一次 send_dict() 会自动重新连接。"""

        with self._send_lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        client_socket = self._socket
        self._socket = None

        if client_socket is None:
            return

        try:
            client_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            client_socket.close()

    def send_dict(self, data: Mapping[str, Any]) -> AckResult | None:
        """
        发送一个包含 Id、plug_info 和 imgbyte 的字典。

        默认返回 None，因为服务端只收不回。开启 ACK 后返回 AckResult。

        这里不会在网络异常后自动重发：sendall() 失败时无法准确判断服务端已经
        收到了多少字节，盲目重发可能造成重复 OCR。调用方可以根据业务需求，
        使用同一个 Id 主动重试，并自行做结果幂等处理。
        """

        message_id, plug_info, image_bytes = self._validate_send_dict(data)
        image_info = inspect_encoded_image(image_bytes)
        plug_info_bytes = self._encode_plug_info(plug_info)

        crc32_value = zlib.crc32(image_bytes) & 0xFFFFFFFF
        timestamp_ms = time.time_ns() // 1_000_000

        header = IMAGE_HEADER.pack(
            MAGIC_IMAGE,
            PROTOCOL_VERSION,
            MESSAGE_TYPE_IMAGE,
            image_info.codec,
            image_info.flags,
            self.client_id.bytes,
            message_id,
            timestamp_ms,
            image_info.width,
            image_info.height,
            len(plug_info_bytes),
            crc32_value,
        )

        body_length = len(header) + len(plug_info_bytes) + len(image_bytes)
        if body_length > MAX_BODY_BYTES:
            raise ValueError(
                f"数据包过大：body={body_length} bytes，"
                f"服务端上限={MAX_BODY_BYTES} bytes"
            )

        # 长度和56字节图片头合并成64字节前导数据。
        # plug_info与大图片分别发送，避免拼接整个数据包产生一次大内存复制。
        frame_prefix = LENGTH_PREFIX.pack(body_length) + header

        with self._send_lock:
            self._connect_unlocked()
            client_socket = self._socket
            assert client_socket is not None

            try:
                # TCP允许拆包；服务端使用readexactly()，因此分三次发送完全正确。
                client_socket.sendall(frame_prefix)
                client_socket.sendall(plug_info_bytes)
                client_socket.sendall(image_bytes)

                l.ilog(
                    f"图片发送完成  {message_id} {len(plug_info_bytes), image_info.format_name,  image_info.width,  image_info.height, len(image_bytes), crc32_value,}"
                )

                if not self.wait_for_ack:
                    return None

                previous_timeout = client_socket.gettimeout()
                client_socket.settimeout(self.ack_timeout)
                try:
                    return self._receive_ack_unlocked(client_socket, message_id)
                finally:
                    client_socket.settimeout(previous_timeout)

            except (OSError, ImageProtocolError):
                # 当前 TCP 字节流可能已经不完整或失去同步，不能继续复用这条连接。
                self._close_unlocked()
                raise

    @staticmethod
    def _validate_send_dict(
        data: Mapping[str, Any],
    ) -> tuple[int, dict[str, Any], bytes]:
        required_keys = ("Id", "plug_info", "imgbyte")
        missing_keys = [key for key in required_keys if key not in data]
        if missing_keys:
            raise KeyError(f"发送字典缺少字段：{missing_keys}")

        message_id = data["Id"]
        plug_info_value = data["plug_info"]
        image_value = data["imgbyte"]

        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise TypeError("data['Id'] 必须是 int")
        if not 0 <= message_id <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("data['Id'] 必须位于 uint64 范围内")

        if not isinstance(plug_info_value, dict):
            raise TypeError("data['plug_info'] 必须是 dict")

        if not isinstance(image_value, bytes):
            raise TypeError("data['imgbyte'] 必须是 bytes")
        if not image_value:
            raise ValueError("data['imgbyte'] 不能为空")

        return message_id, plug_info_value, image_value

    @staticmethod
    def _encode_plug_info(plug_info: dict[str, Any]) -> bytes:
        """把自定义信息编码为紧凑 UTF-8 JSON，并限制最大体积。"""

        try:
            plug_info_bytes = json.dumps(
                plug_info,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "data['plug_info'] 必须只包含可JSON序列化的数据"
            ) from exc

        if len(plug_info_bytes) > MAX_PLUG_INFO_BYTES:
            raise ValueError(
                f"plug_info编码后过大：{len(plug_info_bytes)} bytes，"
                f"上限={MAX_PLUG_INFO_BYTES} bytes"
            )

        return plug_info_bytes

    def _receive_ack_unlocked(
        self,
        client_socket: socket.socket,
        expected_message_id: int,
    ) -> AckResult:
        prefix = self._recv_exact(client_socket, LENGTH_PREFIX.size)
        body_length = LENGTH_PREFIX.unpack(prefix)[0]

        if body_length != ACK_BODY.size:
            raise ImageProtocolError(
                f"ACK 长度错误：expected={ACK_BODY.size}, actual={body_length}"
            )

        body = self._recv_exact(client_socket, body_length)
        (
            magic,
            version,
            message_type,
            status,
            _flags,
            client_id_bytes,
            message_id,
            server_timestamp_ms,
            detail_code,
            received_bytes,
        ) = ACK_BODY.unpack(body)

        if magic != MAGIC_ACK:
            raise ImageProtocolError(f"ACK magic 错误：{magic!r}")
        if version != PROTOCOL_VERSION or message_type != MESSAGE_TYPE_ACK:
            raise ImageProtocolError(
                f"ACK 协议错误：version={version}, type={message_type}"
            )
        if uuid.UUID(bytes=client_id_bytes) != self.client_id:
            raise ImageProtocolError("ACK 的 client_id 与当前客户端不一致")
        if message_id != expected_message_id:
            raise ImageProtocolError(
                f"ACK 的 message_id 不一致：expected={expected_message_id}, "
                f"actual={message_id}"
            )

        ack_result = AckResult(
            status=status,
            message_id=message_id,
            server_timestamp_ms=server_timestamp_ms,
            detail_code=detail_code,
            received_bytes=received_bytes,
        )

        l.ilog(
            f"收到服务端ACK  {  ack_result.message_id}{ ack_result.status}{ ack_result.received_bytes}"
        )
        return ack_result

    @staticmethod
    def _recv_exact(client_socket: socket.socket, byte_count: int) -> bytes:
        """准确读取指定字节数，解决 TCP 一次 recv() 可能只返回部分数据的问题。"""

        result = bytearray(byte_count)
        view = memoryview(result)
        received = 0

        while received < byte_count:
            count = client_socket.recv_into(view[received:])
            if count == 0:
                raise ConnectionError("等待服务端 ACK 时连接已关闭")
            received += count

        return bytes(result)

    # def __enter__(self) -> ImageTcpClient:
    #     self.connect()
    #     return self
    #
    # def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
    #     self.close()


# 每隔多少秒检查一次当前聚焦窗口。
CAPTURE_INTERVAL_SECONDS = 3.0

# 窗口移动达到该距离（像素）才认为位置发生有效变化。
POSITION_CHANGE_THRESHOLD_PIXELS = 50

# 64位感知 dHash 至少有这么多 bit 不同时，才认为画面明显变化。
VISUAL_HASH_DISTANCE_THRESHOLD = 8

# 是否将已成功发送的变化截图额外保存为 PNG；网络传输始终只使用内存 WEBP。
SAVE_CAPTURE_IMAGES = Const.save_capture_images

# 上一次 Hash 保存在程序启动时的当前目录中；截图保存开关不影响它。
SAVE_DIRECTORY = STATE_DIR / "foreground_window_captures"
LAST_HASH_FILE = STATE_DIR / "last_hash.json"


def calculate_dhash(rgb_bytes: bytes, image_size: tuple[int, int]) -> str:
    """生成 64 位 dHash，忽略小范围像素闪烁和轻微抗锯齿变化。"""
    image = Image.frombytes("RGB", image_size, rgb_bytes)
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    gray_image = image.convert("L").resize((9, 8), resampling)

    hash_value = 0
    for y in range(8):
        for x in range(8):
            hash_value <<= 1
            if gray_image.getpixel((x, y)) > gray_image.getpixel((x + 1, y)):
                hash_value |= 1

    return f"{hash_value:016x}"


def hamming_distance(first_hash: str, second_hash: str) -> int:
    """返回两个 64 位 dHash 中不同 bit 的数量。"""
    return (int(first_hash, 16) ^ int(second_hash, 16)).bit_count()


user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


def enable_dpi_awareness():
    """避免Windows缩放比例导致窗口坐标和实际截图像素不一致。"""
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def get_window_capture_area(hwnd):
    """返回当前窗口在整个多显示器桌面中的截图区域。"""
    rect = wintypes.RECT()

    # DWMWA_EXTENDED_FRAME_BOUNDS = 9
    # 这个边界比普通GetWindowRect更接近用户实际看到的窗口边缘。
    result = dwmapi.DwmGetWindowAttribute(
        hwnd,
        9,
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )

    if result != 0:
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None

    # 多显示器虚拟桌面的真实边界，坐标可能是负数。
    virtual_left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    virtual_top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
    virtual_width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    virtual_height = user32.GetSystemMetrics(79) # SM_CYVIRTUALSCREEN

    virtual_right = virtual_left + virtual_width
    virtual_bottom = virtual_top + virtual_height

    # 窗口有一部分超出屏幕时，只截取仍处于屏幕内的区域。
    left = max(rect.left, virtual_left)
    top = max(rect.top, virtual_top)
    right = min(rect.right, virtual_right)
    bottom = min(rect.bottom, virtual_bottom)

    width = right - left
    height = bottom - top

    if width <= 0 or height <= 0:
        return None

    return {
        "left": left,
        "top": top,
        "width": width,
        "height": height,
    }


def main(stop_file: Path | None = None):
    def stopping() -> bool:
        return stop_file is not None and stop_file.exists()

    while True:
        if stopping():
            l.ilog("收到停止请求，Agent 在认证前退出。")
            return
        try:
            respon_json = phone_verify_password()
            code=respon_json.get("code",-111)

            if code==0:
                l.ilog(f'初始化认证通过 {respon_json}')
                break
            else:
                l.ilog(f'初始化认证响应出错了 {respon_json}')
        except Exception as e:
            l.ilog(f'初始化认证 注册 设备的时候出错了. {e}')
        time.sleep(30)
    threading.Thread(target=loop_verify, daemon=True).start()
    enable_dpi_awareness()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if SAVE_CAPTURE_IMAGES:
        SAVE_DIRECTORY.mkdir(parents=True, exist_ok=True)

    # 从文件读取上一次成功发送的窗口状态。旧版纯 Hash 文件会自动失效并首帧发送。
    previous_state = None
    if LAST_HASH_FILE.exists():
        try:
            saved_state = json.loads(
                LAST_HASH_FILE.read_text(encoding="utf-8")
            )
            required_keys = {"left", "top", "width", "height", "dhash"}
            if (
                isinstance(saved_state, dict)
                and required_keys.issubset(saved_state)
                and all(
                    isinstance(saved_state[key], int)
                    for key in ("left", "top", "width", "height")
                )
                and isinstance(saved_state["dhash"], str)
            ):
                previous_state = saved_state
        except (OSError, json.JSONDecodeError):
            previous_state = None

    print("截图保存目录：", SAVE_DIRECTORY)
    print("截图间隔：", CAPTURE_INTERVAL_SECONDS, "秒")
    print("鼠标捕获：关闭")
    print("按 Ctrl+C 停止程序")
    socket_client = ImageTcpClient()
    try:
        socket_client.connect()
    except OSError as exc:
        l.elog(f"初始连接图片服务器失败，后续发送时会自动重连：{exc!r}")

    # 同一客户端的消息 Id 必须单调递增。发送失败的同一画面会复用其 Id 重试。
    next_message_id = time.time_ns()
    retry_signature = ""
    retry_message_id: int | None = None

    # with mss.MSS(with_cursor=False) as screen_capture:
    with mss.MSS( ) as screen_capture:
        # mss默认不截取鼠标，这里再次明确关闭。


        try:
            while True:
                if stopping():
                    l.ilog("收到停止请求，Agent 正常退出。")
                    return
                started_at = time.monotonic()

                hwnd = user32.GetForegroundWindow()

                if not hwnd or not user32.IsWindow(hwnd):
                    print("没有获取到有效的当前聚焦窗口")
                elif not user32.IsWindowVisible(hwnd):
                    print("当前聚焦窗口不可见，本轮跳过")
                elif user32.IsIconic(hwnd):
                    print("当前聚焦窗口已最小化，本轮跳过")
                else:
                    capture_area = get_window_capture_area(hwnd)

                    if capture_area is None:
                        print("无法获取当前窗口的有效截图区域")
                    else:
                        frame = screen_capture.grab(capture_area)

                        # 截图期间如果用户切换了窗口，丢弃这一帧。
                        current_hwnd = user32.GetForegroundWindow()
                        current_area = get_window_capture_area(hwnd)

                        if current_hwnd != hwnd:
                            print("截图期间焦点发生变化，本轮跳过")
                        elif current_area != capture_area:
                            print("截图期间窗口位置或大小发生变化，本轮跳过")
                        else:
                            # dHash 只比较画面的大致结构，避免少量像素变化频繁发送。
                            current_visual_hash = calculate_dhash(
                                frame.rgb,
                                frame.size,
                            )
                            current_state = {
                                "left": capture_area["left"],
                                "top": capture_area["top"],
                                "width": frame.width,
                                "height": frame.height,
                                "dhash": current_visual_hash,
                            }

                            should_send = previous_state is None
                            if previous_state is not None:
                                width_changed = (
                                    current_state["width"]
                                    != previous_state["width"]
                                )
                                height_changed = (
                                    current_state["height"]
                                    != previous_state["height"]
                                )
                                move_x = (
                                    current_state["left"]
                                    - previous_state["left"]
                                )
                                move_y = (
                                    current_state["top"]
                                    - previous_state["top"]
                                )
                                moved_enough = (
                                    move_x * move_x + move_y * move_y
                                    >= POSITION_CHANGE_THRESHOLD_PIXELS
                                    * POSITION_CHANGE_THRESHOLD_PIXELS
                                )
                                visual_difference = hamming_distance(
                                    current_state["dhash"],
                                    previous_state["dhash"],
                                )
                                visual_changed_enough = (
                                    visual_difference
                                    >= VISUAL_HASH_DISTANCE_THRESHOLD
                                )
                                should_send = (
                                    width_changed
                                    or height_changed
                                    or moved_enough
                                    or visual_changed_enough
                                )

                            if not should_send:
                                print(
                                    datetime.now().strftime("%H:%M:%S"),
                                    "窗口位置和画面变化未达到发送阈值，跳过发送",
                                )
                            else:
                                current_signature = json.dumps(
                                    current_state,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                                if (
                                    current_signature == retry_signature
                                    and retry_message_id is not None
                                ):
                                    message_id = retry_message_id
                                else:
                                    message_id = next_message_id
                                    next_message_id += 1
                                    retry_signature = current_signature
                                    retry_message_id = message_id

                                try:
                                    image = Image.frombytes(
                                        "RGB", frame.size, frame.rgb
                                    )
                                    buffer = io.BytesIO()
                                    image.save(
                                        buffer,
                                        format="WEBP",
                                        lossless=True,
                                        quality=100,
                                        method=4,
                                    )
                                    webp_bytes = buffer.getvalue()
                                    socket_client.send_dict({
                                        "Id": message_id,
                                        "plug_info": TEST_PLUG_INFO,
                                        "imgbyte": webp_bytes,
                                    })
                                except (
                                    ImageProtocolError,
                                    OSError,
                                    ValueError,
                                ) as exc:
                                    l.elog(   f"截图发送失败 Id={message_id}，"
                                        f"下一轮将重试：{exc!r}"
                                    )
                                    print(
                                        datetime.now().strftime("%H:%M:%S"),
                                        f"截图发送失败 Id={message_id}",
                                    )
                                else:
                                    process_id = wintypes.DWORD()
                                    user32.GetWindowThreadProcessId(
                                        hwnd,
                                        ctypes.byref(process_id),
                                    )

                                    if SAVE_CAPTURE_IMAGES:
                                        timestamp = datetime.now().strftime(
                                            "%Y%m%d_%H%M%S_%f"
                                        )
                                        file_name = (
                                            f"capture_{timestamp}"
                                            f"_pid{process_id.value}"
                                            f"_hwnd{int(hwnd)}"
                                            f"_{current_visual_hash[:12]}.png"
                                        )
                                        file_path = SAVE_DIRECTORY / file_name
                                        try:
                                            mss.tools.to_png(
                                                frame.rgb,
                                                frame.size,
                                                output=str(file_path),
                                            )
                                        except OSError as exc:
                                            l.elog(
                                                f"截图已发送但本地保存失败：{exc!r}"
                                            )
                                        else:
                                            print(
                                                datetime.now().strftime("%H:%M:%S"),
                                                "截图已发送并保存：",
                                                file_path.name,
                                            )

                                    try:
                                        LAST_HASH_FILE.write_text(
                                            json.dumps(
                                                current_state,
                                                ensure_ascii=False,
                                                separators=(",", ":"),
                                            ),
                                            encoding="utf-8",
                                        )
                                    except OSError as exc:
                                        l.elog(f"写入截图 Hash 失败：{exc!r}")
                                    previous_state = current_state
                                    retry_signature = ""
                                    retry_message_id = None

                                    print(
                                        datetime.now().strftime("%H:%M:%S"),
                                        f"画面发生变化，已发送 Id={message_id}，",
                                        f"WEBP={len(webp_bytes)} 字节",
                                    )

                elapsed = time.monotonic() - started_at
                time.sleep(
                    max(0.0, Const.interel - elapsed)
                )

        except KeyboardInterrupt:
            l.ilog("\n程序已停止")
        finally:
            socket_client.close()


if __name__ == "__main__":
    import argparse

    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--agent", action="store_true")
    argument_parser.add_argument("--stop-file", type=Path)
    arguments = argument_parser.parse_args()
    main(arguments.stop_file)
