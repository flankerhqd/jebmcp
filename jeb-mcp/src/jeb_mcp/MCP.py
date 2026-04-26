# -*- coding: utf-8 -*-

import inspect
import json
import os
import re
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
import BaseHTTPServer
import SocketServer

from urlparse import urlparse
from jarray import zeros

from java.io import File
from java.lang import System as JavaSystem

from com.pnfsoftware.jeb.client.api import IScript
from com.pnfsoftware.jeb.core import Artifact, RuntimeProjectUtil
from com.pnfsoftware.jeb.core.actions import (
    ActionContext,
    ActionCommentData,
    ActionOverridesData,
    Actions,
    ActionXrefsData,
)
from com.pnfsoftware.jeb.core.input import FileInput
from com.pnfsoftware.jeb.core.output.text import TextDocumentUtil
from com.pnfsoftware.jeb.core.units.code.android import IApkUnit
from com.pnfsoftware.jeb.core.util import DecompilerHelper


# Python 2.7 doesn't have typing, so we define minimal substitutes
class Any(object):
    pass


def get_type_hints(func):
    """
    Mock for get_type_hints for Python 2.7 / Jython compatibility.
    Extracts positional arg names via inspect.getargspec.
    """
    hints = {}

    if hasattr(func, "__annotations__"):
        hints.update(getattr(func, "__annotations__", {}))

    try:
        args, varargs, keywords, defaults = inspect.getargspec(func)
    except Exception:
        return hints

    for arg in args:
        if arg == "self":
            continue
        if arg not in hints:
            hints[arg] = Any

    return hints


class ErrorMessages:
    SUCCESS = "[Success]"
    MISSING_PARAM = "[Error] Missing parameter."
    LOAD_APK_FAILED = "[Error] Load apk failed."
    LOAD_APK_NOT_FOUND = "[Error] Apk file not found."
    GET_MANIFEST_FAILED = "[Error] Get AndroidManifest text failed."
    INDEX_OUT_OF_BOUNDS = "[Error] Index out of bounds."
    DECOMPILE_FAILED = "[Error] Failed to decompile code."
    METHOD_NOT_FOUND = "[Error] Method not found in current apk, use check_java_identifier tool check your input first."
    METHOD_NOT_FOUND_WITHOUT_CHECK = "[Error] Method not found in current apk."
    CLASS_NOT_FOUND = "[Error] Class not found in current apk, use check_java_identifier tool check your input first."
    CLASS_NOT_FOUND_WITHOUT_CHECK = "[Error] Class not found in current apk."
    FIELD_NOT_FOUND = "[Error] Field not found in current apk, use check_java_identifier tool check your input first."
    FIELD_NOT_FOUND_WITHOUT_CHECK = "[Error] Field not found in current apk."
    RESOURCE_NOT_FOUND = "[Error] Resource not found."
    ADDRESS_NOT_FOUND = "[Error] Address not found in code unit."
    VAR_NOT_FOUND = "[Error] Variable not found in pseudo-code."


class JSONRPCError(Exception):
    def __init__(self, code, message, data=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.data = data


class RPCRegistry(object):
    def __init__(self):
        self.methods = {}

    def register(self, func):
        self.methods[func.__name__] = func
        return func

    def dispatch(self, method, params):
        if method not in self.methods:
            raise JSONRPCError(-32601, u"Method '{0}' not found".format(method))

        func = self.methods[method]
        hints = get_type_hints(func)

        # Remove return annotation if present
        if "return" in hints:
            hints.pop("return", None)

        # Python 2.7 兼容性：统一将字符串参数转换为 unicode
        def to_unicode(v):
            if isinstance(v, str):
                try:
                    return v.decode("utf-8")
                except Exception:
                    return v.decode("utf-8", "ignore")
            return v

        # 获取参数默认值信息以支持可选参数
        try:
            _args, _, _, _defaults = inspect.getargspec(func)
            _args = [a for a in _args if a != "self"]
            _num_defaults = len(_defaults) if _defaults else 0
            _num_required = len(_args) - _num_defaults
        except Exception:
            _args = list(hints.keys())
            _num_required = len(_args)

        def validate_list(params, hints):
            if len(params) < _num_required or len(params) > len(_args):
                raise JSONRPCError(
                    -32602,
                    u"Expected {0}-{1} args, got {2}".format(
                        _num_required, len(_args), len(params)
                    ),
                )
            return [to_unicode(p) for p in params]

        def validate_dict(params, hints):
            extra = set(params.keys()) - set(hints.keys())
            if extra:
                raise JSONRPCError(-32602, u"Unexpected params: {0}".format(list(extra)))
            # 检查必需参数是否存在
            required_keys = set(_args[:_num_required])
            missing = required_keys - set(params.keys())
            if missing:
                raise JSONRPCError(
                    -32602, u"Missing required params: {0}".format(list(missing))
                )
            return {k: to_unicode(params[k]) for k in params}

        if isinstance(params, list):
            return func(*validate_list(params, hints))
        elif isinstance(params, dict):
            return func(**validate_dict(params, hints))
        else:
            raise JSONRPCError(
                -32600, "Invalid Request: params must be array or object"
            )


rpc_registry = RPCRegistry()


def jsonrpc(func):
    """Decorator to register a function as a JSON-RPC method"""
    global rpc_registry
    return rpc_registry.register(func)


class JSONRPCRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    def send_jsonrpc_error(self, code, message, id=None):
        response = {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
        }
        if id is not None:
            response["id"] = id
        response_body = json.dumps(response)
        if isinstance(response_body, unicode):
            response_body = response_body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(response_body))
        self.end_headers()
        self.wfile.write(response_body)

    def do_POST(self):
        global rpc_registry

        parsed_path = urlparse(self.path)
        if parsed_path.path != "/mcp":
            self.send_jsonrpc_error(-32098, "Invalid endpoint", None)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_jsonrpc_error(-32700, "Parse error: missing request body", None)
            return

        if content_length > 10 * 1024 * 1024:  # 10MB limit
            self.send_jsonrpc_error(-32600, "Request too large", None)
            return

        request_body = self.rfile.read(content_length)
        try:
            request = json.loads(request_body)
        except ValueError:  # Python 2.7 uses ValueError instead of JSONDecodeError
            self.send_jsonrpc_error(-32700, "Parse error: invalid JSON", None)
            return

        # Prepare the response
        response = {"jsonrpc": "2.0"}
        if request.get("id") is not None:
            response["id"] = request.get("id")

        try:
            # Basic JSON-RPC validation
            if not isinstance(request, dict):
                raise JSONRPCError(-32600, "Invalid Request")
            if request.get("jsonrpc") != "2.0":
                raise JSONRPCError(-32600, "Invalid JSON-RPC version")
            if "method" not in request:
                raise JSONRPCError(-32600, "Method not specified")

            # Dispatch the method
            result = rpc_registry.dispatch(request["method"], request.get("params", []))
            response["result"] = result

        except JSONRPCError as e:
            response["error"] = {"code": e.code, "message": e.message}
            if e.data is not None:
                response["error"]["data"] = e.data
        except Exception:
            traceback.print_exc()
            response["error"] = {
                "code": -32603,
                "message": "Internal error (please report a bug)",
                "data": traceback.format_exc(),
            }

        try:
            response_body = json.dumps(response)
        except Exception:
            traceback.print_exc()
            # fallback: format_exc as string but safely decode it
            tb = traceback.format_exc()
            try:
                tb_safe = tb.decode("utf-8", "ignore")
            except Exception:
                tb_safe = "Un-decodable traceback"

            response_body = json.dumps(
                {
                    "error": {
                        "code": -32603,
                        "message": "Internal error (please report a bug)",
                        "data": tb_safe,
                    }
                }
            )

        if isinstance(response_body, unicode):
            response_body = response_body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(response_body))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format, *args):
        # Suppress logging
        pass


class MCPHTTPServer(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):
    allow_reuse_address = True


class Server(object):  # Use explicit inheritance from object for py2
    HOST = os.getenv("JEB_MCPC_HOST", "127.0.0.1")
    PORT = int(os.getenv("JEB_MCPC_PORT", "16161"))

    def __init__(self):
        self.server = None
        self.server_thread = None
        self.running = False

    def start(self):
        if self.running:
            print("[MCP] Server is already running")
            return

        # Python 2.7 doesn't support daemon parameter in Thread constructor
        self.server_thread = threading.Thread(target=self._run_server)
        self.server_thread.daemon = True  # Set daemon attribute after creation
        self.running = True
        self.server_thread.start()

    def stop(self):
        if not self.running:
            return

        self.running = False
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.server_thread:
            self.server_thread.join()
            self.server = None
        print("[MCP] Server stopped")

    def _run_server(self):
        try:
            # Create server in the thread to handle binding
            self.server = MCPHTTPServer(
                (Server.HOST, Server.PORT), JSONRPCRequestHandler
            )
            print(
                u"[MCP] Server started at http://{0}:{1}".format(
                    Server.HOST, Server.PORT
                )
            )
            self.server.serve_forever()
        except OSError as e:
            if e.errno == 98 or e.errno == 10048:  # Port already in use (Linux/Windows)
                print(u"[MCP] Error: Port {0} is already in use".format(Server.PORT))
            else:
                print(u"[MCP] Server error: {0}".format(e))
            self.running = False
        except Exception as e:
            print(u"[MCP] Server error: {0}".format(e))
        finally:
            self.running = False


# 定义为 unicode 字符串 (u'...')
def preprocess_manifest_py2(manifest_text):
    """
    一个为 Python 2 设计的、健壮的 Manifest 预处理函数。
    它会清理非法字符，并强行移除所有 <meta-data> 标签以避免解析错误。
    """
    # 1. 确保输入是 unicode 字符串，并忽略解码错误
    if isinstance(manifest_text, str):
        try:
            manifest_text = manifest_text.decode("utf-8")
        except UnicodeDecodeError:
            manifest_text = manifest_text.decode("utf-8", "ignore")

    # 2. 清理基本的非法 XML 字符
    # (保留这个作为基础卫生措施)
    cleaned_chars = []
    for char in manifest_text:
        codepoint = ord(char)
        if (
            codepoint == 0x9
            or codepoint == 0xA
            or codepoint == 0xD
            or (codepoint >= 0x20 and codepoint <= 0xD7FF)
            or (codepoint >= 0xE000 and codepoint <= 0xFFFD)
            or (codepoint >= 0x10000 and codepoint <= 0x10FFFF)
        ):
            cleaned_chars.append(char)
    text_no_illegal_chars = "".join(cleaned_chars)

    # 3. 使用正则表达式，强行移除所有 <meta-data ... /> 标签
    # re.DOTALL 使得 '.' 可以匹配包括换行在内的任意字符
    # re.IGNORECASE 忽略大小写
    # ur'...' 定义一个 unicode 正则表达式
    text_no_metadata = re.sub(
        r"<\s*meta-data.*?/>",
        "",  # 替换为空字符串，即直接删除
        text_no_illegal_chars,
        flags=re.DOTALL | re.IGNORECASE,
    )

    return text_no_metadata


@jsonrpc
def ping():
    """Do a simple ping to check server is alive and running"""
    return "pong"


# implement a FIFO queue to store the artifacts
artifactQueue = list()


def addArtifactToQueue(artifact):
    """Add an artifact to the queue"""
    artifactQueue.append(artifact)


def getArtifactFromQueue():
    """Get an artifact from the queue"""
    if len(artifactQueue) > 0:
        return artifactQueue.pop(0)
    return None


def clearArtifactQueue():
    """Clear the artifact queue"""
    global artifactQueue
    artifactQueue = list()


MAX_OPENED_ARTIFACTS = 1

# 全局缓存管理 (LRU 思想)
# 限制缓存条目数量以保护内存
MAX_CACHE_ENTRIES = 10
apk_cached_data = {}
apk_cache_order = []
_cache_lock = threading.Lock()


def _add_to_cache(key, value):
    """添加数据到缓存并维护顺序，超过限制时弹出最早的数据（线程安全）"""
    with _cache_lock:
        if key in apk_cached_data:
            apk_cache_order.remove(key)
        elif len(apk_cached_data) >= MAX_CACHE_ENTRIES:
            oldest = apk_cache_order.pop(0)
            print(u"[MCP] Cache eviction: popping %s" % oldest)
            del apk_cached_data[oldest]

        apk_cached_data[key] = value
        apk_cache_order.append(key)


def _get_from_cache(key):
    """从缓存获取数据并将 key 移至最新（线程安全）"""
    with _cache_lock:
        if key in apk_cached_data:
            apk_cache_order.remove(key)
            apk_cache_order.append(key)
            return apk_cached_data[key]
        return None


def clear_apk_cache():
    """清理所有缓存（线程安全）"""
    with _cache_lock:
        apk_cached_data.clear()
        del apk_cache_order[:]


def getOrLoadApk(filepath):
    engctx = CTX.getEnginesContext()
    if not engctx:
        print("Back-end engines not initialized")
        raise JSONRPCError(-1, ErrorMessages.LOAD_APK_FAILED)

    if not filepath:
        # 尝试返回当前已经在 JEB 中打开的活动 APK
        projects = engctx.getProjects()
        if projects and len(projects) > 0:
            prj = projects[0]

            # 使用 JEB 官方工具寻找已经建立连接的 APK 单元
            apks = RuntimeProjectUtil.findUnitsByType(prj, IApkUnit, False)
            if apks and len(apks) > 0:
                print("[MCP] No filepath provided, returning active APK unit.")
                return apks[0]

            # Fallback 策略
            for artifact in prj.getLiveArtifacts():
                unit = artifact.getMainUnit()
                if isinstance(unit, IApkUnit):
                    print("[MCP] No filepath provided, returning active APK artifact.")
                    return unit
        raise JSONRPCError(
            -1,
            "[Error] No active APK currently opened in JEB, please specify filepath.",
        )

    if not os.path.exists(filepath):
        print(u"File not found: %s" % filepath)
        raise JSONRPCError(-1, ErrorMessages.LOAD_APK_NOT_FOUND)

    # Load or create the project in the same directory as the APK file
    project_path = filepath + ".jdb2"
    project = engctx.loadProject(project_path)
    correspondingArtifact = None
    for artifact in project.getLiveArtifacts():
        if artifact.getArtifact().getName() == filepath:
            # If the artifact is already loaded, return it
            correspondingArtifact = artifact
            break

    if correspondingArtifact:
        # Update its position in the queue to mark it as most recently used
        if correspondingArtifact in artifactQueue:
            artifactQueue.remove(correspondingArtifact)
            artifactQueue.append(correspondingArtifact)
    else:
        # try to load the artifact, but first check if the queue size has been exceeded
        while len(artifactQueue) >= MAX_OPENED_ARTIFACTS:
            # unload the oldest artifact
            oldestArtifact = getArtifactFromQueue()
            if oldestArtifact:
                # unload the artifact
                oldestArtifactName = oldestArtifact.getArtifact().getName()
                print(
                    u"Unloading artifact: %s because queue size limit exceeded"
                    % oldestArtifactName
                )
                try:
                    RuntimeProjectUtil.destroyLiveArtifact(oldestArtifact)
                except Exception as e:
                    print(u"[MCP] Error destroying artifact: %s" % str(e))

        # Fix: 直接用filepath而不是basename作为Artifact的名称，否则如果加载了多个同名不同路径的apk，会出现问题。
        correspondingArtifact = project.processArtifact(
            Artifact(filepath, FileInput(File(filepath)))
        )
        if not correspondingArtifact:
            raise JSONRPCError(-1, ErrorMessages.LOAD_APK_FAILED)
        addArtifactToQueue(correspondingArtifact)
        clear_apk_cache()

    unit = correspondingArtifact.getMainUnit()
    if isinstance(unit, IApkUnit):
        # If the unit is already loaded, return it
        return unit
    raise JSONRPCError(-1, ErrorMessages.LOAD_APK_FAILED)


@jsonrpc
def get_manifest(filepath):
    """Get the manifest of the given APK file in path, note filepath needs to be an absolute path"""

    # Use optimized cache
    cache_key = "manifest_" + filepath
    cached_text = _get_from_cache(cache_key)
    if cached_text:
        return cached_text

    apk = getOrLoadApk(filepath)
    man = apk.getManifest()
    if man is None:
        raise JSONRPCError(-1, ErrorMessages.GET_MANIFEST_FAILED)

    text = _extract_text_content(man)
    if text is None:
        raise JSONRPCError(-1, ErrorMessages.GET_MANIFEST_FAILED)

    _add_to_cache(cache_key, text)
    return text


@jsonrpc
def get_exported_components(filepath, component_type):
    """
    Get all exported components of the specified type from the APK manifest.
    A component is considered "exported" if:
    - It explicitly sets android:exported="true", or
    - It omits android:exported but includes an <intent-filter> (implicitly exported).
    component_type can be: 'activity', 'service', 'receiver', 'provider'.
    Returns a list of fully qualified exported component class names.
    """
    if not component_type:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    # 校验组件类型
    valid_types = ("activity", "service", "receiver", "provider")
    if component_type not in valid_types:
        raise JSONRPCError(
            -1,
            "[Error] Invalid component_type. Must be one of: " + ", ".join(valid_types),
        )

    cache_key = "exported_" + component_type + "s_" + filepath

    # 首先尝试在缓存中取，跳过XML解析。
    cached = _get_from_cache(cache_key)
    if cached is not None:
        return cached

    manifest_text = get_manifest(filepath)
    manifest_text = preprocess_manifest_py2(manifest_text)

    if not manifest_text:
        raise JSONRPCError(-1, ErrorMessages.GET_MANIFEST_FAILED)

    try:
        root = ET.fromstring(manifest_text.encode("utf-8"))
    except Exception as e:
        print("[MCP] Error parsing manifest:", e)
        raise JSONRPCError(-1, ErrorMessages.GET_MANIFEST_FAILED)

    ANDROID_NS = "http://schemas.android.com/apk/res/android"
    exported_components = []

    # 获取包名
    package_name = root.attrib.get("package", "").strip()

    # 查找 <application> 节点
    app_node = root.find("application")
    if app_node is None:
        raise JSONRPCError(-1, ErrorMessages.GET_MANIFEST_FAILED)

    for node in app_node.findall(component_type):
        name = node.attrib.get("{" + ANDROID_NS + "}name")
        exported = node.attrib.get("{" + ANDROID_NS + "}exported")
        has_intent_filter = len(node.findall("intent-filter")) > 0

        if not name:
            continue

        if exported == "true" or (exported is None and has_intent_filter):
            normalized = []
            seen = set()

            def _add_unique(val):
                if val not in seen:
                    seen.add(val)
                    normalized.append(val)

            if name.startswith("."):
                _add_unique(package_name + name)
            elif "." not in name:
                _add_unique(name)
                _add_unique(package_name + "." + name)
            else:
                _add_unique(name)

            exported_components.extend(normalized)

    # 缓存数据
    _add_to_cache(cache_key, exported_components)
    return exported_components


@jsonrpc
def get_smali_code(filepath, item_signature):
    """Get the smali code of the given class or method in the APK file.
    The passed in item_signature needs to be a fully-qualified signature.
    Dex units use Java-style internal addresses to identify items:
    - package: Lcom/abc/
    - type: Lcom/abc/Foo;
    - method: Lcom/abc/Foo;->bar(I[JLjava/Lang/String;)V
    - field: Lcom/abc/Foo;->flag1:Z
    note filepath needs to be an absolute path
    """
    if not item_signature:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()

    # Optimized: Find item using unified identifier/signature logic
    item, item_type = find_item_by_signature(codeUnit, item_signature)
    if not item:
        # Check if it was a class or method for proper error message
        if "->" in item_signature:
            print(u"Method not found: {0}".format(item_signature).encode("utf-8"))
            raise_method_not_found(item_signature)
        else:
            print(u"Class not found: {0}".format(item_signature).encode("utf-8"))
            raise_class_not_found(item_signature)

    if item_type == "method":
        instructions = item.getInstructions()
        lines = []
        for instruction in instructions:
            lines.append(instruction.format(None))
        return "\n".join(lines)
    elif item_type == "class":
        lines = []
        for method in item.getMethods():
            lines.append("method: " + method.getSignature(True))
            instructions = method.getInstructions()
            for instruction in instructions:
                lines.append(instruction.format(None))
            lines.append("")
        return "\n".join(lines)

    return ""


@jsonrpc
def get_decompiled_code(filepath, item_signature):
    """Get the decompiled code of the given class or method in the APK file.
    The passed in item_signature needs to be a fully-qualified signature.
    Dex units use Java-style internal addresses to identify items:
    - package: Lcom/abc/
    - type: Lcom/abc/Foo;
    - method: Lcom/abc/Foo;->bar(I[JLjava/Lang/String;)V
    - field: Lcom/abc/Foo;->flag1:Z
    note filepath needs to be an absolute path
    """
    if not item_signature:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()
    decomp = DecompilerHelper.getDecompiler(codeUnit)
    if not decomp:
        print(
            u"Cannot acquire decompiler for unit: {0}".format(codeUnit).encode("utf-8")
        )
        raise JSONRPCError(-1, ErrorMessages.DECOMPILE_FAILED)

    # Optimized: Find item using unified identifier/signature logic
    item, item_type = find_item_by_signature(codeUnit, item_signature)
    if not item:
        if "->" in item_signature:
            print(u"Method not found: {0}".format(item_signature).encode("utf-8"))
            raise_method_not_found(item_signature)
        else:
            print(u"Class not found: {0}".format(item_signature).encode("utf-8"))
            raise_class_not_found(item_signature)

    if item_type == "method":
        if not decomp.decompileMethod(item.getSignature()):
            print(
                u"Failed decompiling method: {0}".format(item_signature).encode("utf-8")
            )
            raise JSONRPCError(-1, ErrorMessages.DECOMPILE_FAILED)
        return decomp.getDecompiledMethodText(item.getSignature())
    elif item_type == "class":
        if not decomp.decompileClass(item.getSignature()):
            print(
                u"Failed decompiling class: {0}".format(item_signature).encode("utf-8")
            )
            raise JSONRPCError(-1, ErrorMessages.DECOMPILE_FAILED)
        return decomp.getDecompiledClassText(item.getSignature())

    return ""


def find_item_by_signature(codeUnit, item_signature):
    """
    Find a code item (Class, Method, or Field) by its fully-qualified signature.
    Returns: (item, type_name) where type_name is 'class', 'method', 'field' or ''.
    """
    if not item_signature:
        return None, ""

    # Harmonize signature to unicode for handles non-ASCII characters in Jython 2.7
    if isinstance(item_signature, str):
        try:
            item_signature = item_signature.decode("utf-8")
        except Exception:
            item_signature = item_signature.decode("utf-8", "ignore")

    item = None
    # 1. Try to determine type by signature pattern
    if item_signature.startswith("L") and item_signature.endswith(";"):
        item = codeUnit.getClass(item_signature)
        if item:
            return item, "class"
    elif "->" in item_signature:
        if "(" in item_signature:
            item = codeUnit.getMethod(item_signature)
            if item:
                return item, "method"
        else:
            item = codeUnit.getField(item_signature)
            if item:
                return item, "field"

    # 2. Generic fallback if pattern didn't match or direct lookup failed
    try:
        for lookup in [codeUnit.getClass, codeUnit.getMethod, codeUnit.getField]:
            item = lookup(item_signature)
            if item:
                return item, lookup.__name__[3:].lower()  # getField -> field
    except Exception:
        pass

    return None, ""


@jsonrpc
def get_method_overrides(filepath, method_signature):
    """
    Get the overrides of the given method in the APK file, the passed in method_signature needs to be a fully-qualified signature
    note filepath needs to be an absolute path
    """
    if not method_signature:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)
    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()
    method, item_type = find_item_by_signature(codeUnit, method_signature)
    if method is None or item_type != "method":
        print(u"Method not found: {0}".format(method_signature).encode("utf-8"))
        raise_method_not_found(method_signature)
    ret = []
    data = ActionOverridesData()
    actionContext = ActionContext(
        codeUnit, Actions.QUERY_OVERRIDES, method.getItemId(), None
    )
    if codeUnit.prepareExecution(actionContext, data):
        for i in range(data.getAddresses().size()):
            ret.append(data.getAddresses()[i])
    return ret


@jsonrpc
def get_class_hierarchy(filepath, class_signature, relation_type):
    """
    Get the superclass or interfaces of the given class in the APK file.
    relation_type: 'superclass' or 'interface'.
    """
    if not class_signature or not relation_type:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()
    clazz, item_type = find_item_by_signature(codeUnit, class_signature)

    if clazz is None or item_type != "class":
        print(u"Class not found: {0}".format(class_signature).encode("utf-8"))
        raise_class_not_found(class_signature)

    if relation_type == "superclass":
        return clazz.getSupertypeSignature(True)
    elif relation_type == "interface":
        return [sig for sig in clazz.getInterfaceSignatures(True)]
    else:
        raise JSONRPCError(
            -1, "[Error] Invalid relation_type. Use 'superclass' or 'interface'."
        )


@jsonrpc
def get_class_members(filepath, class_signature, member_type):
    """
    Get the members (methods or fields) of the given class in the APK file.
    member_type: 'method' or 'field'.
    """
    if not class_signature or not member_type:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()
    clazz, item_type = find_item_by_signature(codeUnit, class_signature)

    if clazz is None or item_type != "class":
        print(u"Class not found: {0}".format(class_signature).encode("utf-8"))
        raise_class_not_found(class_signature)

    ret = []
    if member_type == "method":
        items = clazz.getMethods()
    elif member_type == "field":
        items = clazz.getFields()
    else:
        raise JSONRPCError(-1, "[Error] Invalid member_type. Use 'method' or 'field'.")

    for item in items:
        if item:
            ret.append(item.getSignature(True))

    return ret


@jsonrpc
def rename_code_item(filepath, item_signature, new_name):
    """
    Rename a code class, method or field to another name, which may be better-suited or more descriptive than the original name.
    """
    if not item_signature or not new_name:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()
    item, item_type = find_item_by_signature(codeUnit, item_signature)

    if item is None:
        print(u"Item not found: {0}".format(item_signature).encode("utf-8"))
        raise JSONRPCError(-1, u"[Error] Item not found: " + item_signature)

    print(u"rename item: {0} to {1}".format(item.getName(), new_name).encode("utf-8"))
    item.setName(new_name)
    return True


def replace_last_once(s, old, new):
    parts = s.rsplit(old, 1)
    return new.join(parts) if len(parts) > 1 else s


@jsonrpc
def check_java_identifier(filepath, identifier):
    """
    Check an identifier in the APK file and recognize if this is a class, method or field.
    the passed in identifier needs to be a fully-qualified name (like `com.abc.def.Foo`) or a signature;
    the passed in filepath needs to be a fully-qualified absolute path;
    the return value will be a list to tell you the possible type of the passed identifier.
    """
    if not identifier:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()

    # Normalize input to signature if needed
    sig = identifier
    if not (sig.startswith("L") and (sig.endswith(";") or ";->" in sig)):
        # Try converting from dot notation to signature notation
        if "." in identifier or not identifier.startswith("L"):
            sig = "L" + identifier.replace(".", "/") + ";"

    # Use index-based lookup via find_item_by_signature for O(1) performance
    item, item_type = find_item_by_signature(codeUnit, sig)

    # If not found with original sig, maybe it's a method without signature details
    if not item and ";" in sig and ";->" not in sig:
        # Try as a partial method match via replace_last_once
        fake_sig = replace_last_once(sig, ";", ";->")
        item, item_type = find_item_by_signature(codeUnit, fake_sig)

    result = []
    if item:
        parent_sig = "N/A"
        if item_type == "class":
            parent_sig = item.getPackage().getSignature(True)
        elif item_type in ("method", "field"):
            parent_sig = item.getClassType().getSignature(True)

        result.append(
            {
                "type": item_type,
                "signature": item.getSignature(True),
                "parent": parent_sig,
            }
        )

    if len(result) == 0:
        if (
            identifier.startswith("dalvik")
            or identifier.startswith("Landroid")
            or identifier.startswith("android")
        ):
            result.append(
                {"type": "Android base type", "signature": "N/A", "parent": "N/A"}
            )
        elif identifier.startswith("Ljava") or identifier.startswith("java"):
            result.append(
                {"type": "Java base type", "signature": "N/A", "parent": "N/A"}
            )
        else:
            result.append({"type": "Not found", "signature": "N/A", "parent": "N/A"})

    return result


def _try_rename_in_java_method(decomp, java_method, old_var_name, new_var_name):
    """
    尝试在一个已反编译的 IJavaMethod 中查找并重命名变量。
    返回 (found: bool, debug_info: list)
    """
    debug_info = []

    # Strategy A: Use IdentifierManager
    try:
        idmgr = java_method.getIdentifierManager()
        if idmgr:
            all_idents = idmgr.getIdentifiers()
            if all_idents:
                for ident in all_idents:
                    defn = idmgr.getDefinition(ident)
                    iname = None
                    if defn and hasattr(defn, "getName"):
                        iname = defn.getName()
                    if not iname:
                        iname = str(ident)

                    if iname == old_var_name:
                        if hasattr(decomp, "setIdentifierName"):
                            try:
                                res = decomp.setIdentifierName(ident, new_var_name)
                                if res:
                                    return True, debug_info
                            except Exception:
                                pass
                        try:
                            if defn and hasattr(defn, "setName"):
                                defn.setName(new_var_name)
                                return True, debug_info
                        except Exception:
                            pass
        else:
            debug_info.append("idmgr=None")
    except Exception as e:
        debug_info.append("idmgr_err=" + str(e))

    # Strategy B: Check method parameters directly (handles p0, p1...)
    try:
        params = java_method.getParameters()
        if params:
            for param in params:
                if param.getIdentifier().getName() == old_var_name:
                    param.getIdentifier().setName(new_var_name)
                    return True, debug_info
    except Exception as e:
        debug_info.append("param_err=" + str(e))

    return False, debug_info


@jsonrpc
def rename_pseudo_code_variables(
    filepath, method_signature, old_var_name, new_var_name
):
    """
    Rename one or more local variables or parameters defined in the decompiled pseudo-code of a method.
    The method must have been decompiled first.
    Also supports renaming variables inside lambda expressions by automatically
    searching synthetic lambda methods in the same class.
    """
    if not method_signature or not old_var_name or not new_var_name:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)

    codeUnit = apk.getDex()
    method = codeUnit.getMethod(method_signature)
    if not method:
        raise_method_not_found(method_signature)

    decomp = DecompilerHelper.getDecompiler(codeUnit)
    if not decomp:
        raise JSONRPCError(-1, ErrorMessages.DECOMPILE_FAILED)

    # Ensure method is decompiled
    method_sig = method.getSignature()
    if not decomp.decompileMethod(method_sig):
        raise JSONRPCError(-1, ErrorMessages.DECOMPILE_FAILED)

    java_method = None
    try:
        java_method = decomp.getMethod(method_sig, False)
    except Exception as e:
        print(u"[MCP] decomp.getMethod failed: {0}".format(e).encode("utf-8"))

    all_debug = []

    # Strategy 1 & 2: Try in the target method itself
    if java_method:
        found, dbg = _try_rename_in_java_method(
            decomp, java_method, old_var_name, new_var_name
        )
        all_debug.extend(dbg)
        if found:
            return True

    # Strategy 3: Search lambda/synthetic methods in the same class
    # Lambda variables belong to compiler-generated methods like lambda$xxx$0
    try:
        # Extract class signature from method signature: "Lcom/Foo;->bar()V" -> "Lcom/Foo;"
        class_sig = method_signature.split("->")[0]
        if not class_sig.endswith(";"):
            class_sig = class_sig + ";"
        dex_class = codeUnit.getClass(class_sig)
        if dex_class:
            class_methods = dex_class.getMethods()
            if class_methods:
                for m in class_methods:
                    m_sig = m.getSignature(True)
                    m_name = m.getName(True)
                    # Only check lambda$ and access$ synthetic methods
                    if not m_name:
                        continue
                    if "lambda$" not in m_name and "access$" not in m_name:
                        continue
                    # Skip the original method itself
                    if m_sig == method_sig:
                        continue
                    try:
                        if not decomp.decompileMethod(m_sig):
                            continue
                        jm = decomp.getMethod(m_sig, False)
                        if not jm:
                            continue
                        found, dbg = _try_rename_in_java_method(
                            decomp, jm, old_var_name, new_var_name
                        )
                        all_debug.extend(dbg)
                        if found:
                            return True
                    except Exception:
                        continue
        else:
            all_debug.append("class_not_found=" + class_sig)

        # Strategy 4: Search inner anonymous classes (e.g. OuterClass$1)
        inner_class_prefix = class_sig[:-1] + "$"
        for clz in codeUnit.getClasses():
            if clz.getSignature(True).startswith(inner_class_prefix):
                for m in clz.getMethods():
                    m_sig = m.getSignature(True)
                    if m_sig == method_sig:
                        continue
                    try:
                        if not decomp.decompileMethod(m_sig):
                            continue
                        jm = decomp.getMethod(m_sig, False)
                        if not jm:
                            continue
                        found, dbg = _try_rename_in_java_method(
                            decomp, jm, old_var_name, new_var_name
                        )
                        # We don't append every single inner class dbg failure to avoid huge error msgs
                        if found:
                            return True
                    except Exception:
                        continue

    except Exception as e:
        all_debug.append("lambda_scan_err=" + str(e))

    msg = u"[Error] Variable '{0}' not found in method {1} (including lambda methods). Debug: {2}".format(
        old_var_name, method_signature, u"; ".join(all_debug)
    )
    raise JSONRPCError(-1, msg)


@jsonrpc
def list_cross_references(filepath, address):
    """Retrieve cross-references to an address in a code unit, that is, the users or callers of the item at the provided address."""
    if not address:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    # Default use the dex unit
    codeUnit = apk.getDex()
    if not codeUnit:
        raise JSONRPCError(-1, "[Error] DEX unit not found.")

    item, item_type = find_item_by_signature(codeUnit, address)

    if not item:
        raise JSONRPCError(-1, ErrorMessages.ADDRESS_NOT_FOUND + " " + address)

    item_id = item.getItemId()
    if item_id <= 0:
        raise JSONRPCError(-1, ErrorMessages.ADDRESS_NOT_FOUND + " " + address)

    ret = []
    actionXrefsData = ActionXrefsData()
    actionContext = ActionContext(codeUnit, Actions.QUERY_XREFS, item_id, None)
    if codeUnit.prepareExecution(actionContext, actionXrefsData):
        for i in range(actionXrefsData.getAddresses().size()):
            ret.append(
                {
                    "address": actionXrefsData.getAddresses()[i],
                    "details": actionXrefsData.getDetails()[i],
                }
            )
    return ret


@jsonrpc
def list_dex_strings(filepath):
    """
    Retrieve the list of strings defined in the dex constants pools.
    """
    pass

    apk = getOrLoadApk(filepath)
    if apk is None:
        return []

    codeUnit = apk.getDex()
    if not codeUnit:
        return []

    strings = codeUnit.getStrings()
    return [s.getValue() for s in strings]


@jsonrpc
def get_all_classes(filepath):
    """
    List all classes in the project (from the Dex unit).
    """
    pass

    apk = getOrLoadApk(filepath)

    codeUnit = apk.getDex()
    if not codeUnit:
        return []

    classes = codeUnit.getClasses()
    return [c.getSignature(True) for c in classes]


def _extract_text_content(unit):
    """
    Unified method to extract text representation from any JEB Unit.
    Handles formatter -> presentation -> document -> text flow.
    """

    if not unit:
        return None

    try:
        formatter = unit.getFormatter()
        if formatter:
            presentation = formatter.getPresentation(0)
            if presentation:
                doc = presentation.getDocument()
                if doc:
                    return TextDocumentUtil.getText(doc)
    except Exception:
        try:
            print("[MCP] Error extracting text: " + str(sys.exc_info()[1]))
        except Exception:
            pass

    # Fallback: try to read raw bytes (up to 1MB)
    stream = None
    try:
        if hasattr(unit, "getInput"):
            inp = unit.getInput()
            if inp:
                stream = inp.getStream()
                if stream:
                    # JEB Input Streams are usually limited length, read it
                    # Jython stream reading might need a byte array
                    length = (
                        stream.available()
                        if hasattr(stream, "available")
                        else 1024 * 1024
                    )
                    if length > 0:
                        length = min(length, 1024 * 1024)
                        buf = zeros(length, "b")
                        read_len = stream.read(buf, 0, length)
                        if read_len > 0:
                            # Convert to Python string
                            raw_str = buf[:read_len].tostring()
                            return raw_str
    except Exception:
        try:
            print("[MCP] Fallback raw read failed: " + str(sys.exc_info()[1]))
        except Exception:
            pass
    finally:
        if stream:
            try:
                stream.close()
            except Exception:
                pass

    return None


def _build_unit_tree_index(root_unit, cache_key):
    """
    Unified iterative DFS to build a path -> Unit index for any tree-like Unit structure.
    Uses LRU caching based on the provided cache_key.
    """
    cached = _get_from_cache(cache_key)
    if cached is not None:
        return cached

    index = {}
    if not root_unit:
        return index

    # Iterative DFS to build index
    stack = [(root_unit, "")]
    while stack:
        current, current_path = stack.pop()
        children = current.getChildren()
        if not children:
            if current_path:
                index[current_path] = current
            continue
        for child in children:
            name = child.getName()
            new_path = (current_path + "/" + name) if current_path else name
            stack.append((child, new_path))

    _add_to_cache(cache_key, index)
    return index


def _find_unit_in_index(index, input_path, prefixes):
    """
    Unified fuzzy path matcher. Supports direct, prefix, and suffix matching.
    """
    if not isinstance(input_path, unicode):
        input_path = input_path.decode("utf-8")

    # Step 1: Direct Matching
    leaf = index.get(input_path)
    if leaf:
        return leaf

    # Step 2: Try stripping user-provided prefixes if they exist in input_path
    for prefix in prefixes:
        if input_path.startswith(prefix):
            stripped_path = input_path[len(prefix) :]
            if stripped_path in index:
                return index[stripped_path]

    # Step 3: Try adding prefixes to match index paths
    for prefix in prefixes:
        candidate = prefix + input_path
        if candidate in index:
            return index[candidate]

    # Step 4: Suffix matching (powerful fallback)
    suffix = input_path if input_path.startswith("/") else ("/" + input_path)
    for path, unit in index.items():
        if path.endswith(suffix):
            return unit

    return None


def _get_resource_index(apk):
    """构建 路径 -> Unit 对象的全量资源索引并缓存"""
    return _build_unit_tree_index(apk.getResources(), "resource_index")


def _get_asset_index(apk):
    """构建 路径 -> Unit 对象的全量素材索引并缓存"""
    return _build_unit_tree_index(apk.getAssets(), "asset_index")


@jsonrpc
def get_apk_all_files(filepath, category):
    """
    Retrieve all file names for a given category (resource or asset) from the application.
    category: 'resource' or 'asset'.
    """
    if not category:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    if category == "resource":
        index = _get_resource_index(apk)
    elif category == "asset":
        index = _get_asset_index(apk)
    else:
        raise JSONRPCError(-1, "[Error] Invalid category. Use 'resource' or 'asset'.")

    return list(index.keys())


@jsonrpc
def get_apk_file_content(filepath, file_path, category):
    """
    Retrieve the text contents of a file (resource or asset) using its path.
    category: 'resource' or 'asset'.
    """
    if not file_path or not category:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    if category == "resource":
        index = _get_resource_index(apk)
        prefixes = ["res/", "Resources/res/", "Resources/"]
    elif category == "asset":
        index = _get_asset_index(apk)
        prefixes = ["assets/", "Resources/assets/", "Resources/"]
    else:
        raise JSONRPCError(-1, "[Error] Invalid category. Use 'resource' or 'asset'.")

    leaf = _find_unit_in_index(index, file_path, prefixes)

    if not leaf:
        msg = u"[Error] {0} not found: ".format(category.capitalize()) + file_path
        raise JSONRPCError(-1, msg)

    content = _extract_text_content(leaf)
    if content is None:
        raise JSONRPCError(
            -1,
            u"[Error] Failed to read {0} content or format not supported.".format(
                category
            ),
        )

    if isinstance(content, str):
        try:
            content = content.decode("utf-8")
        except Exception:
            content = content.decode("utf-8", "ignore")
    elif not isinstance(content, unicode):
        try:
            content = unicode(content)
        except Exception:
            pass

    return content


@jsonrpc
def add_comment(filepath, address, comment):
    """
    Add a comment to function, class, field or any address in a code unit.
    The address can be a signature (e.g., Lcom/abc/Foo;->bar()V) or a virtual address.
    """
    if not address or comment is None:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()

    # Support offset syntax: Lcom/abc/Foo;->bar()V+0x10
    lookup_address = address
    offset_str = ""
    if "->" in address and "+" in address:
        parts = address.split("+")
        lookup_address = parts[0]
        offset_str = parts[1]

    item, item_type = find_item_by_signature(codeUnit, lookup_address)

    if item:
        # Align instruction boundary if providing an offset for a method
        if item_type == "method" and offset_str:
            try:
                # strip potential 'h' suffix and parse as hex
                raw_hex = offset_str.lower().replace("h", "").replace("0x", "")
                req_offset = int(raw_hex, 16)

                instructions = item.getInstructions()
                if instructions:
                    valid_offsets = [inst.getOffset() for inst in instructions]
                    if req_offset not in valid_offsets:
                        closest_offset = 0
                        for off in valid_offsets:
                            if off <= req_offset:
                                closest_offset = off
                            else:
                                break
                        # Fix the address to the valid boundary
                        address = lookup_address + u"+0x{0:x}".format(closest_offset)
                        print(
                            "[MCP] add_comment: requested offset {0:x} is inside an instruction. Auto-aligned to boundary {1:x} ({2})".format(
                                req_offset, closest_offset, address
                            ).encode("utf-8")
                        )
            except Exception as e:
                print(
                    "[MCP] add_comment boundary check failed: {0}".format(e).encode(
                        "utf-8"
                    )
                )

        itemId = item.getItemId()
        # Using string formatting carefully for Python 2.7 unicode
        print(
            u"[MCP] add_comment: item found, itemId={0}, address={1}".format(
                itemId, address
            )
        )

        # JEB comment workflow: prepare -> set -> execute
        data = ActionCommentData()
        # Using the original 'address' which contains the +offset if provided
        act_ctx = ActionContext(codeUnit, Actions.COMMENT, itemId, address)
        if codeUnit.prepareExecution(act_ctx, data):
            data.setNewComment(comment)
            if codeUnit.executeAction(act_ctx, data):
                return True

        # Fallback: trying with address only if it looks like a hex/dec address
        data2 = ActionCommentData()
        act_ctx2 = ActionContext(codeUnit, Actions.COMMENT, 0, address)
        if codeUnit.prepareExecution(act_ctx2, data2):
            data2.setNewComment(comment)
            if codeUnit.executeAction(act_ctx2, data2):
                return True

        msg = u"[Error] Failed to add comment to item: " + address
        raise JSONRPCError(-1, msg)
    else:
        # Handle decimal or hex virtual addresses directly

        try:
            # Check if address is numeric (dec or hex)
            if address.lower().startswith("0x"):
                int(address, 16)
            else:
                int(address)

            data = ActionCommentData()
            act_ctx = ActionContext(codeUnit, Actions.COMMENT, 0, address)
            if codeUnit.prepareExecution(act_ctx, data):
                data.setNewComment(comment)
                if codeUnit.executeAction(act_ctx, data):
                    return True
        except ValueError:
            pass

        msg = u"[Error] Could not resolve address or signature: " + address
        raise JSONRPCError(-1, msg)


def _search_in_file_index(index, query, category):
    """
    在文件索引(resource/asset)中搜索路径和文本内容。
    - 路径匹配: 返回 {"type": "path", "category": ..., "path": ...}
    - 内容匹配: 返回 {"type": "content", "category": ..., "path": ..., "matches": [...]}
    """
    TEXT_EXTENSIONS = (
        ".xml",
        ".json",
        ".txt",
        ".html",
        ".htm",
        ".css",
        ".js",
        ".properties",
        ".cfg",
        ".ini",
        ".yml",
        ".yaml",
        ".csv",
        ".smali",
        ".pro",
        ".gradle",
        ".md",
    )
    results = []

    for path, unit in index.items():
        # 1. 路径匹配
        if query in path:
            results.append({"type": "path", "category": category, "path": path})

        # 2. 文本内容匹配 (仅对文本类文件)
        path_lower = path.lower()

        # 优化：通过后缀快速过滤
        is_text = False
        for ext in TEXT_EXTENSIONS:
            if path_lower.endswith(ext):
                is_text = True
                break

        if is_text:
            try:
                # 检查输入源大小以避免内存暴涨
                if hasattr(unit, "getInput"):
                    inp = unit.getInput()
                    if inp and inp.getSize() > 2 * 1024 * 1024:  # 2MB 限制
                        continue

                content = _extract_text_content(unit)
                if not content:
                    continue

                if isinstance(content, str):
                    try:
                        content = content.decode("utf-8")
                    except Exception:
                        content = content.decode("utf-8", "ignore")

                if query in content:
                    matching_lines = []
                    lines = content.split("\n")
                    # 限制结果行数避免前端拥堵
                    for i, line in enumerate(lines, 1):
                        current_line = line.strip()
                        if query in current_line:
                            matching_lines.append(u"L{0}: {1}".format(i, current_line))
                            if len(matching_lines) > 50:  # 每个文件最多匹配 50 行
                                matching_lines.append("... (too many matches)")
                                break
                    if matching_lines:
                        results.append(
                            {
                                "type": "content",
                                "category": category,
                                "path": path,
                                "matches": matching_lines,
                            }
                        )
            except Exception:
                pass

    return results


def _append_file_search_results(results, file_results, unit_label):
    """将 _search_in_file_index 的结果转换并追加到 results 列表中"""
    for r in file_results:
        if r["type"] == "content":
            for m in r["matches"]:
                results.append(
                    {
                        "Text": m,
                        "Unit": unit_label,
                        "Document": r["path"],
                        "Location": r["path"],
                    }
                )
        elif r["type"] == "path":
            results.append(
                {
                    "Text": r["path"],
                    "Unit": unit_label,
                    "Document": "Directory/File",
                    "Location": r["path"],
                }
            )


@jsonrpc
def search_in_project(filepath, query, search_type="string"):
    """
    Search for strings or identifiers (classes/methods) in the project.
    search_type can be 'string' (default), 'identifier', 'resource', or 'asset'.
    - 'string': search DEX string pool for matching values.
    - 'identifier': search class/method signatures.
    - 'resource': search resource file paths and text content (xml, json, etc).
    - 'asset': search asset file paths and text content.
    """
    if not query:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    results = []

    if search_type == "string":
        codeUnit = apk.getDex()
        if codeUnit:
            unit_name = (
                codeUnit.getName() if hasattr(codeUnit, "getName") else "Bytecode"
            )
            for s in codeUnit.getStrings():
                val = s.getValue()
                if val and query in val:
                    loc_addr = (
                        u"0x{0:X}".format(s.getItemId())
                        if hasattr(s, "getItemId")
                        else u""
                    )
                    results.append(
                        {
                            "Text": val,
                            "Unit": unit_name,
                            "Document": "String pool",
                            "Location": loc_addr,
                        }
                    )

        # Search resources and assets to mimic 'Entire Project' search
        _append_file_search_results(
            results,
            _search_in_file_index(_get_resource_index(apk), query, "resource"),
            "Resources",
        )
        _append_file_search_results(
            results,
            _search_in_file_index(_get_asset_index(apk), query, "asset"),
            "Assets",
        )

    elif search_type == "identifier":
        codeUnit = apk.getDex()
        if not codeUnit:
            raise JSONRPCError(-1, "[Error] DEX unit not found.")
        unit_name = codeUnit.getName() if hasattr(codeUnit, "getName") else "Bytecode"
        for c in codeUnit.getClasses():
            sig = c.getSignature(True)
            if query in sig:
                results.append(
                    {
                        "Text": sig,
                        "Unit": unit_name,
                        "Document": "Class",
                        "Location": sig,
                    }
                )
        for m in codeUnit.getMethods():
            sig = m.getSignature(True)
            if query in sig:
                results.append(
                    {
                        "Text": sig,
                        "Unit": unit_name,
                        "Document": "Method",
                        "Location": sig,
                    }
                )

    elif search_type == "resource":
        _append_file_search_results(
            results,
            _search_in_file_index(_get_resource_index(apk), query, "resource"),
            "Resources",
        )
    elif search_type == "asset":
        _append_file_search_results(
            results,
            _search_in_file_index(_get_asset_index(apk), query, "asset"),
            "Assets",
        )
    else:
        raise JSONRPCError(
            -1,
            "[Error] Invalid search_type. Use 'string', 'identifier', 'resource', or 'asset'.",
        )

    return results


# 规则文件缓存，避免每次扫描都重新读取磁盘
_rules_file_cache = {}


def _load_json_rules(path):
    """
    Load JSON rules from a file, compatible with Jython 2.7.
    Results are cached to avoid redundant disk I/O.
    """
    if path in _rules_file_cache:
        return _rules_file_cache[path]

    if not os.path.exists(path):
        _rules_file_cache[path] = None
        return None
    try:
        with open(path, "rb") as f:
            content = f.read()
            try:
                content = content.decode("utf-8")
            except Exception:
                content = content.decode("utf-8", "ignore")
            data = json.loads(content)
            _rules_file_cache[path] = data
            return data
    except Exception as e:
        print(u"[MCP] Error loading JSON rules from {0}: {1}".format(path, e))
        _rules_file_cache[path] = None
        return None


def _scan_apk_for_packers(apk):
    """
    Internal helper to scan APK units for packer signatures (File-based).
    Inspired by ApkCheckPack.
    """
    results = []
    script_dir = os.path.dirname(os.path.abspath(__file__))
    rules_dir = os.path.join(script_dir, "rules")

    # Load rules
    # from https://github.com/moyuwa/ApkCheckPack/tree/main/data
    apkpack_data = _load_json_rules(os.path.join(rules_dir, "apkpackdata.json"))
    sdk_data = _load_json_rules(os.path.join(rules_dir, "sdk.json"))

    # Collect all file paths and potentially contents in APK
    all_paths = set()
    all_contents_list = []  # Use a list for safer joining

    # Iterative DFS to collect all unit paths (avoids stack overflow on deep trees)
    stack = [(apk, "")]
    while stack:
        current_unit, current_path = stack.pop()
        try:
            name = current_unit.getName()
            if name:
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "ignore")
                elif not isinstance(name, unicode):
                    name = unicode(name)

                all_paths.add(name)
                # Full composite path
                full_p = (current_path + "/" + name) if current_path else name
                all_paths.add(full_p)

                # Content extraction
                if (
                    name.endswith(".xml")
                    or name.endswith(".txt")
                    or name.endswith(".json")
                ):
                    # 防止由于恶意打包大尺寸文本导致 Jython 内存溢出
                    if hasattr(current_unit, "getInput"):
                        inp = current_unit.getInput()
                        if inp and inp.getSize() > 2 * 1024 * 1024:
                            continue

                    content = _extract_text_content(current_unit)
                    if content:
                        if isinstance(content, str):
                            content = content.decode("utf-8", "ignore")
                        all_contents_list.append(content)
            else:
                full_p = current_path

            children = current_unit.getChildren()
            if children:
                for child in children:
                    stack.append((child, full_p))
        except Exception:
            pass

    all_contents_combined = u"\n".join(all_contents_list)
    print(
        u"[MCP] Collected {0} paths and content length {1}".format(
            len(all_paths), len(all_contents_combined)
        )
    )

    # Match against apkpackdata.json
    if apkpack_data:
        for packer_name, rules in apkpack_data.items():
            hit = False
            matched_feature = None

            # soname
            sonames = rules.get("soname", [])
            for sn in sonames:
                for ap in all_paths:
                    if sn in ap:
                        hit = True
                        matched_feature = "SO: " + sn
                        break
                if hit:
                    break

            # other (files)
            if not hit:
                others = rules.get("other", [])
                for ot in others:
                    if ot in all_paths:
                        hit = True
                        matched_feature = "File: " + ot
                        break

            # Match content
            if not hit and all_contents_combined:
                others = rules.get("other", [])
                for ot in others:
                    if ot in all_contents_combined:
                        hit = True
                        matched_feature = "Content: " + ot
                        break

                if not hit:
                    keywords = rules.get("keywords", [])
                    for kw in keywords:
                        if kw in all_contents_combined:
                            hit = True
                            matched_feature = "Keyword: " + kw
                            break

            if hit:
                results.append(
                    {
                        u"category": u"Packer (ApkCheckPack)",
                        u"name": packer_name,
                        u"detail": matched_feature,
                    }
                )

    # Match against sdk.json (List of dicts: {"soname": "...", "zh": {"label": "..."}})
    if sdk_data:
        for sdk_entry in sdk_data:
            sn = sdk_entry.get("soname")
            if not sn:
                continue

            hit = False
            for ap in all_paths:
                if sn in ap:
                    hit = True
                    break

            if hit:
                label = (
                    sdk_entry.get(u"zh", {}).get(u"label")
                    or sdk_entry.get(u"en", {}).get(u"label")
                    or sn
                )
                dev = (
                    sdk_entry.get(u"zh", {}).get(u"dev_team")
                    or sdk_entry.get(u"en", {}).get(u"dev_team")
                    or u"Unknown"
                )
                results.append(
                    {
                        u"category": u"SDK",
                        u"name": label,
                        u"detail": u"Team: {0} (Match: {1})".format(dev, sn),
                    }
                )

    return results


_u_apis_cache = None
_u_apis_regex_cache = None


def _load_sensitive_apis():
    global _u_apis_cache, _u_apis_regex_cache
    if _u_apis_cache is not None:
        return _u_apis_cache, _u_apis_regex_cache

    script_dir = os.path.dirname(os.path.abspath(__file__))
    rules_dir = os.path.join(script_dir, "rules")
    config_path = os.path.join(rules_dir, "sensitive_strings.txt")

    u_apis = []
    if os.path.exists(config_path):
        try:
            with open(config_path, "rb") as f:
                for line_b in f:
                    line_b = line_b.strip()
                    if line_b and not line_b.startswith(b"#"):
                        try:
                            line_str = line_b.decode("utf-8")
                        except Exception:
                            line_str = line_b.decode("utf-8", "ignore")
                        u_apis.append(line_str)
        except Exception as e:
            print(u"[MCP] Error reading sensitive APIs: {0}".format(e))

    _u_apis_cache = u_apis
    if u_apis:
        pattern = "|".join(re.escape(r) for r in u_apis)
        _u_apis_regex_cache = re.compile("(" + pattern + ")")
    else:
        _u_apis_regex_cache = None

    return _u_apis_cache, _u_apis_regex_cache


@jsonrpc
def perform_security_scan(filepath):
    """
    Performs a comprehensive security scan on the APK, covering three areas:
    1. Packer Detection: Identifies known packers/protectors via file signature matching (rules from 'apkpackdata.json').
    2. SDK Identification: Detects embedded third-party SDKs by matching native library names (rules from 'sdk.json').
    3. Sensitive String/API Scan: Searches the DEX string pool, method and class signatures for sensitive patterns (rules from 'sensitive_strings.txt').
    Returns a flat list of dicts, each with 'type' ('Packer', 'SDK', or 'Sensitive String'), 'name', and 'detail'.
    """
    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()

    # 1. Load sensitive APIs (cached)
    u_apis, regex = _load_sensitive_apis()

    # 2. Packer & SDK Scan
    print(u"[MCP] Phase 1/4: Scanning file signatures for Packers and SDKs...")
    all_file_results = _scan_apk_for_packers(apk)

    packer_results = [
        r for r in all_file_results if r[u"category"] == u"Packer (ApkCheckPack)"
    ]
    sdk_results = [r for r in all_file_results if r[u"category"] == u"SDK"]

    # 3. DEX Sensitive Scan
    dex_results = []
    if codeUnit:
        print(u"[MCP] Phase 2/4: Scanning DEX string pool...")
        dex_hits = {}

        # Pull data once
        all_dex_content = []
        for s in codeUnit.getStrings():
            all_dex_content.append(s.getValue())
        for m in codeUnit.getMethods():
            all_dex_content.append(m.getSignature(True))
        for c in codeUnit.getClasses():
            all_dex_content.append(c.getSignature(True))

        print(u"[MCP] Phase 3/4: Matching rules against DEX content...")
        # 优化：使用预编译的正则全局对象
        if regex:
            for content in all_dex_content:
                if not content:
                    continue
                # findall 返回所有非重叠匹配
                matches = regex.findall(content)
                if matches:
                    for rule in set(matches):
                        if rule not in dex_hits:
                            dex_hits[rule] = set()
                        dex_hits[rule].add(content)

        print(u"[MCP] Phase 4/4: Finalizing DEX results...")
        for rule in u_apis:
            if rule in dex_hits:
                unique_matches = list(dex_hits[rule])
                dex_results.append(
                    {
                        "category": "Sensitive API/String",
                        "name": rule,
                        "count": len(unique_matches),
                        "matches": unique_matches[:10],
                    }
                )

    # Combine results into a flat list as required by the return type list[dict]
    final_results = []

    for p in packer_results:
        final_results.append(
            {u"type": u"Packer", u"name": p[u"name"], u"detail": p[u"detail"]}
        )

    for s in sdk_results:
        final_results.append({u"type": u"SDK", u"name": s[u"name"], u"detail": s[u"detail"]})

    for d in dex_results:
        final_results.append(
            {
                u"type": u"Sensitive String",
                u"name": d[u"name"],
                u"detail": u"Hits: {0}".format(d[u"count"]),
            }
        )

    return final_results


@jsonrpc
def export_all_resources(filepath, output_dir=""):
    """
    Export all accessible resources and assets to a local directory structure.
    If output_dir is empty, defaults to a 'dump' directory next to the APK.
    """
    apk = getOrLoadApk(filepath)

    if not output_dir:
        # Determine the default output directory from the active Artifact
        apk_path = None
        engctx = CTX.getEnginesContext()
        if engctx and engctx.getProjects():
            prj = engctx.getProjects()[0]
            for art in prj.getLiveArtifacts():
                if art.getMainUnit() == apk:
                    apk_path = art.getArtifact().getName()
                    break

        if apk_path and os.path.exists(apk_path):
            base = os.path.splitext(apk_path)[0]
            output_dir = base + "_dump"
        else:
            output_dir = os.path.join(os.getcwd(), "dump")

    # 1. Export Resources
    res_index = _get_resource_index(apk)
    # 2. Export Assets
    asset_index = _get_asset_index(apk)

    total = len(res_index) + len(asset_index)
    success = 0

    # helper for exporting
    def save_units(index, base_name):
        import java.lang.Throwable
        from java.io import FileOutputStream

        curr_success = 0
        for path, unit in index.items():
            try:
                # Sanitize path for local FS
                rel_path = path.replace("/", os.sep)
                # 过滤 Windows 非法路径字符
                for c in '<>:"|?*':
                    rel_path = rel_path.replace(c, "_")

                full_path = os.path.join(output_dir, base_name, rel_path)

                p_dir = os.path.dirname(full_path)
                if not os.path.exists(p_dir):
                    os.makedirs(p_dir)

                # 优先：直接调用 Java 原生 IO 缓冲流将文件推到磁盘，绕过 Jython 字符串内存分配，防止超大体积 OOM
                has_streamed = False
                if hasattr(unit, "getInput"):
                    inp = unit.getInput()
                    if inp:
                        stream = inp.getStream()
                        if stream:
                            fos = FileOutputStream(full_path)
                            buffer = zeros(16384, "b")  # 16KB block
                            while True:
                                read_bytes = stream.read(buffer)
                                if read_bytes <= 0:
                                    break
                                fos.write(buffer, 0, read_bytes)
                            fos.close()
                            stream.close()
                            has_streamed = True
                            curr_success += 1

                if has_streamed:
                    continue

                # 降级备用：如果不是物理文件源，则尝试使用内部文字提取并序列化
                content = _extract_text_content(unit)
                if content:
                    with open(full_path, "wb") as f:
                        if isinstance(content, unicode):
                            f.write(content.encode("utf-8"))
                        else:
                            f.write(content)
                    curr_success += 1
            except java.lang.Throwable:
                pass
            except Exception:
                pass
        return curr_success

    success += save_units(res_index, "res")
    success += save_units(asset_index, "assets")

    return {
        "status": "success",
        "total": total,
        "exported": success,
        "output_dir": output_dir,
    }


def _is_platform_type(signature):
    """判断签名是否属于 Android/Java/Dalvik 平台类型"""
    return (
        signature.startswith("Ldalvik")
        or signature.startswith("Ljava")
        or signature.startswith("Landroid")
    )


def raise_class_not_found(class_signature):
    if _is_platform_type(class_signature):
        raise JSONRPCError(-1, ErrorMessages.CLASS_NOT_FOUND_WITHOUT_CHECK)
    raise JSONRPCError(-1, ErrorMessages.CLASS_NOT_FOUND)


def raise_method_not_found(method_signature):
    if _is_platform_type(method_signature):
        raise JSONRPCError(-1, ErrorMessages.METHOD_NOT_FOUND_WITHOUT_CHECK)
    raise JSONRPCError(-1, ErrorMessages.METHOD_NOT_FOUND)


def raise_field_not_found(field_signature):
    if _is_platform_type(field_signature):
        raise JSONRPCError(-1, ErrorMessages.FIELD_NOT_FOUND_WITHOUT_CHECK)
    raise JSONRPCError(-1, ErrorMessages.FIELD_NOT_FOUND)


CTX = None

# ---------------------------------------------------------------------------
# 热重载支持
# ---------------------------------------------------------------------------
# 核心问题：JEB 每次 Run Script 时, Jython 会重新执行整个模块, 产生新的
# rpc_registry / Server 实例。但旧的 HTTP Server 线程仍在运行并占用端口。
#
# 解决方案：利用 Java 的 System Properties 在 JVM 级别保存旧 Server 的引用。
# 脚本被重新执行时, 可以通过同一个 property key 找到并关闭旧 Server, 然后
# 启动携带最新代码的新 Server，无需重启 JEB。
# ---------------------------------------------------------------------------


_MCP_SERVER_PROP_KEY = "__jeb_mcp_server_instance__"


def _stop_previous_server():
    """
    尝试关闭上一次脚本运行时遗留的 HTTP Server。
    通过 Java System Properties 存取跨模块加载的 Server 引用。
    """
    try:
        old_server = JavaSystem.getProperties().get(_MCP_SERVER_PROP_KEY)
        if old_server is not None:
            print("[MCP] Hot-reload: stopping previous server...")
            old_server.stop()
            JavaSystem.getProperties().remove(_MCP_SERVER_PROP_KEY)
            # 给操作系统一点时间释放端口
            time.sleep(0.3)
            print("[MCP] Hot-reload: previous server stopped.")
    except Exception as e:
        print(u"[MCP] Hot-reload: failed to stop previous server: {0}".format(e))


def _save_server_reference(server):
    """将当前 Server 实例保存到 Java System Properties 中，供下次热重载使用。"""
    JavaSystem.getProperties().put(_MCP_SERVER_PROP_KEY, server)


class MCP(IScript):
    def __init__(self):
        self.server = None
        print(u"[MCP] Plugin loaded")

    def run(self, ctx):
        global CTX
        CTX = ctx

        # 1. 关闭上一次运行遗留的旧 Server (热重载核心)
        _stop_previous_server()

        # 2. 清理缓存确保状态一致
        clear_apk_cache()
        clearArtifactQueue()

        # 3. 启动新 Server (此时 rpc_registry 已包含最新的函数定义)
        self.server = Server()
        self.server.start()
        _save_server_reference(self.server)
        print(u"[MCP] Plugin running (hot-reload ready)")

        is_daemon = int(os.getenv("JEB_MCP_DAEMON", "0"))
        if is_daemon == 1:
            try:
                while True:
                    time.sleep(10)
            except KeyboardInterrupt:
                print("Exiting...")

    def term(self):
        if self.server:
            self.server.stop()
        JavaSystem.getProperties().remove(_MCP_SERVER_PROP_KEY)
