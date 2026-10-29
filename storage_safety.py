"""Fail-closed exclusions for C-drive storage cleanup candidates."""
import json
import ntpath
import os
import re
import stat
import threading
from pathlib import Path


_AI_APP_MARKERS = {
    "openai", "chatgpt", "anthropic", "claude", "gemini", "deepseek", "doubao",
    "kimi", "moonshot", "qwen", "tongyi", "yuanbao", "wenxin", "yiyan",
    "hunyuan", "zhipu", "chatglm", "modelscope", "huggingface", "ollama",
    "lmstudio", "anythingllm", "gpt4all", "comfyui", "stablediffusion",
    "automatic1111", "invokeai", "fooocus", "diffusionbee", "oobabooga",
    "textgenerationwebui", "koboldcpp", "cursor", "windsurf", "copilot",
    "continue", "tabby", "dify", "coze", "jmai", "codex",
}
_AI_PROJECT_MARKERS = {
    "ai", "aiproject", "aiprojects", "aimodel", "aimodels", "aicache",
    "aiworkspace", "llm", "llmproject", "llmprojects", "llmcache",
    "model", "models", "checkpoint", "checkpoints", "weight", "weights",
    "lora", "loras", "embedding", "embeddings", "\.cache",
}
_MODEL_SUFFIXES = {".gguf", ".ggml", ".safetensors", ".ckpt", ".pth", ".pt", ".onnx", ".tflite", ".mlmodel"}
_MAX_STORAGE_CLEANUP_IDS = 3000
_AI_EXCLUSION_LOCK = threading.RLock()


def _components(path):
    if not isinstance(path, (str, bytes)) or not path:
        return None, None
    try:
        text = ntpath.normpath(ntpath.expandvars(str(path))).replace("/", "\\")
    except (TypeError, ValueError, OSError):
        return None, None
    parts = [part for part in text.split("\\") if part and part not in (".", "..")]
    compact = [re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", part.casefold()) for part in parts]
    return text.casefold(), compact


def _default_exclusion_store():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "LxUltra" / "ai_protected_paths.json"


def _normalized_exclusion(path, require_existing=False):
    if not isinstance(path, (str, os.PathLike)):
        return ""
    raw = os.path.expandvars(os.path.expanduser(str(path).strip().strip('"')))
    if not raw:
        return ""
    normalized = ntpath.normpath(raw.replace("/", "\\"))
    drive, tail = ntpath.splitdrive(normalized)
    if not drive or not tail.startswith("\\") or tail.rstrip("\\") == "":
        return ""
    if require_existing:
        try:
            info = os.stat(normalized, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return ""
        except (OSError, ValueError, TypeError):
            return ""
    return normalized.rstrip("\\")


def _load_ai_exclusions(store_path=None):
    store = Path(store_path) if store_path is not None else _default_exclusion_store()
    with _AI_EXCLUSION_LOCK:
        try:
            raw = json.loads(store.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    result, seen = [], set()
    for value in raw[:500]:
        normalized = _normalized_exclusion(value)
        key = ntpath.normcase(normalized) if normalized else ""
        if normalized and key not in seen:
            result.append(normalized)
            seen.add(key)
    return result


def _save_ai_exclusions(paths, store_path=None):
    store = Path(store_path) if store_path is not None else _default_exclusion_store()
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        temporary = store.with_suffix(store.suffix + ".tmp")
        temporary.write_text(json.dumps(paths, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, store)
        return True
    except (OSError, ValueError, TypeError):
        return False


def is_ai_protected_path(path, extra_paths=None):
    """True for known AI app/cache/model/project paths; unknown paths fail closed."""
    text, parts = _components(path)
    if text is None or not parts:
        return True
    if extra_paths is None:
        extra_paths = _load_ai_exclusions()
    candidate = ntpath.normcase(ntpath.normpath(text))
    for protected_root in extra_paths:
        try:
            root = ntpath.normcase(ntpath.normpath(protected_root))
            if ntpath.commonpath((candidate, root)) == root:
                return True
        except (OSError, ValueError, TypeError):
            continue
    if any(part in _AI_APP_MARKERS or part in _AI_PROJECT_MARKERS for part in parts):
        return True
    if any(part.startswith(("aiproject", "aimodel", "aicache", "llmproject", "llmcache")) for part in parts):
        return True
    if any(part.endswith(suffix) for part in parts for suffix in ("models", "modelcache")):
        return True
    leaf = ntpath.basename(text)
    if ntpath.splitext(leaf)[1] in _MODEL_SUFFIXES:
        return True
    # Protect model artifacts even when the extension is generic.
    if any(marker in text for marker in ("\\.cache\\huggingface\\", "\\.cache\\torch\\",
                                          "\\.cache\\transformers\\", "\\.cache\\diffusers\\",
                                          "\\.cache\\modelscope\\", "\\.ollama\\models\\",
                                          "\\.lmstudio\\models\\")):
        return True
    return False


def _ai_protected_directory(path, extra_paths):
    """Return the most useful protected directory for a hidden AI candidate."""
    if not isinstance(path, (str, bytes)) or not path:
        return None, None
    try:
        candidate = ntpath.normpath(ntpath.expandvars(str(path))).replace("/", "\\")
        candidate_key = ntpath.normcase(candidate)
        for protected_root in extra_paths:
            root = _normalized_exclusion(protected_root)
            if root and ntpath.commonpath((candidate_key, ntpath.normcase(root))) == ntpath.normcase(root):
                return root, "custom"
    except (OSError, ValueError, TypeError):
        pass

    drive, tail = ntpath.splitdrive(candidate)
    parts = [part for part in tail.split("\\") if part]
    compact = [re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", part.casefold()) for part in parts]
    marker_index = next(
        (index for index, part in enumerate(compact)
         if part in _AI_APP_MARKERS or part in _AI_PROJECT_MARKERS
         or part.startswith(("aiproject", "aimodel", "aicache", "llmproject", "llmcache"))),
        None,
    )
    if marker_index is not None:
        return ntpath.join(drive + "\\", *parts[:marker_index + 1]), "automatic"
    leaf = ntpath.basename(candidate)
    if ntpath.splitext(leaf)[1].casefold() in _MODEL_SUFFIXES:
        parent = ntpath.dirname(candidate)
        return (parent, "automatic") if parent else (None, None)
    return None, None


def filter_storage_status(state, extra_paths=None):
    """Split auto-detected AI candidates into an opt-in list and hide custom protections."""
    if not isinstance(state, dict):
        return state
    result = dict(state)
    items = state.get("items")
    if not isinstance(items, list):
        return result
    protected_paths = _load_ai_exclusions() if extra_paths is None else extra_paths
    visible, detected, detected_count, detected_bytes = [], [], 0, 0
    protected_count, protected_bytes = 0, 0
    protected_directories = {}
    detected_directories = {}
    for item in items:
        if not isinstance(item, dict):
            protected_count += 1
            continue
        path = item.get("path")
        text, _ = _components(path)
        if text is None:
            protected_count += 1
            continue
        try:
            size = max(0, int(item.get("size", 0)))
        except (TypeError, ValueError, OverflowError):
            size = 0
        directory, source = _ai_protected_directory(path, protected_paths)
        if source == "custom":
            protected_count += 1
            protected_bytes += size
            if directory:
                key = ntpath.normcase(directory)
                entry = protected_directories.setdefault(key, {
                    "path": directory, "count": 0, "bytes": 0, "source": "custom",
                })
                entry["count"] += 1
                entry["bytes"] += size
            continue
        if is_ai_protected_path(path, []):
            candidate = dict(item)
            candidate["original_category"] = item.get("category")
            candidate["category"] = "ai"
            candidate["ai_recognized"] = True
            detected.append(candidate)
            detected_count += 1
            detected_bytes += size
            if directory:
                key = ntpath.normcase(directory)
                entry = detected_directories.setdefault(key, {
                    "path": directory, "count": 0, "bytes": 0, "source": "automatic",
                })
                entry["count"] += 1
                entry["bytes"] += size
            continue
        visible.append(item)
    result["items"] = visible
    result["ai_detected_items"] = detected
    result["ai_detected_count"] = detected_count
    result["ai_detected_bytes"] = detected_bytes
    result["ai_detected_directories"] = sorted(
        detected_directories.values(), key=lambda entry: (-entry["bytes"], entry["path"].casefold()))[:100]
    result["ai_protected_count"] = protected_count
    result["ai_protected_bytes"] = protected_bytes
    result["ai_protected_directories"] = sorted(
        protected_directories.values(), key=lambda entry: (-entry["bytes"], entry["path"].casefold()))[:100]
    return result


def install_storage_guards(api_class, store_path=None):
    """Filter scan results and re-check the live scan snapshot before cleanup."""
    if api_class.__dict__.get("_lx_storage_safety_installed", False):
        return False
    original_status = getattr(api_class, "storage_scan_status", None)
    original_cleanup = getattr(api_class, "storage_cleanup", None)
    if not callable(original_status) or not callable(original_cleanup):
        return False

    def storage_scan_status(self):
        return filter_storage_status(original_status(self), _load_ai_exclusions(store_path))

    def storage_ai_exclusions_list(self):
        return {"ok": True, "paths": _load_ai_exclusions(store_path)}

    def storage_ai_exclusions_add(self, path):
        normalized = _normalized_exclusion(path, require_existing=True)
        if not normalized:
            return {"ok": False, "error": "请选择一个存在的普通文件夹，不能选择磁盘根目录或目录联接。"}
        with _AI_EXCLUSION_LOCK:
            paths = _load_ai_exclusions(store_path)
            if any(ntpath.normcase(item) == ntpath.normcase(normalized) for item in paths):
                return {"ok": True, "paths": paths, "message": "该目录已经受到保护。"}
            if len(paths) >= 500:
                return {"ok": False, "error": "AI 保护目录数量已达上限。"}
            paths.append(normalized)
            if not _save_ai_exclusions(paths, store_path):
                return {"ok": False, "error": "无法保存 AI 保护目录，请检查本机应用数据权限。"}
        return {"ok": True, "paths": paths, "message": "此目录及其内容已加入 AI 项目保护。"}

    def storage_ai_exclusions_remove(self, path):
        normalized = _normalized_exclusion(path)
        if not normalized:
            return {"ok": False, "error": "保护目录路径无效。"}
        with _AI_EXCLUSION_LOCK:
            paths = [item for item in _load_ai_exclusions(store_path)
                     if ntpath.normcase(item) != ntpath.normcase(normalized)]
            if not _save_ai_exclusions(paths, store_path):
                return {"ok": False, "error": "无法更新 AI 保护目录，请稍后重试。"}
        return {"ok": True, "paths": paths, "message": "已移除该 AI 保护目录。"}

    def storage_cleanup(self, identifiers, allow_ai=False):
        if not isinstance(identifiers, list) or not identifiers or len(identifiers) > _MAX_STORAGE_CLEANUP_IDS:
            return {"ok": False, "error": "清理选择无效，请重新扫描并逐项选择。"}
        if any(not isinstance(identity, str) or not identity or len(identity) > 128 for identity in identifiers):
            return {"ok": False, "error": "清理项目标识无效，请重新扫描。"}
        if len(set(identifiers)) != len(identifiers):
            return {"ok": False, "error": "清理列表包含重复项目，请重新扫描。"}
        try:
            state = original_status(self)
        except Exception as exc:
            return {"ok": False, "error": "无法复核当前扫描结果，请重新扫描后再清理。"}
        if not isinstance(state, dict) or state.get("running"):
            return {"ok": False, "error": "扫描尚未完成或结果不可用，请完成扫描后再清理。"}
        items = state.get("items")
        if not isinstance(items, list):
            return {"ok": False, "error": "扫描结果已失效，请重新扫描后再清理。"}
        by_id = {item.get("id"): item for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)}
        selected = [by_id.get(identity) for identity in identifiers]
        if any(item is None for item in selected):
            return {"ok": False, "error": "部分项目已不在当前扫描结果中，请重新扫描。"}
        if not isinstance(allow_ai, bool):
            return {"ok": False, "error": "AI 文件清理确认无效，请重新勾选并确认。"}
        protected_paths = _load_ai_exclusions(store_path)
        custom_protected = [item for item in selected
                            if _ai_protected_directory(item.get("path"), protected_paths)[1] == "custom"]
        if custom_protected:
            return {"ok": False, "error": "已阻止操作：这些文件位于你手动保护的目录中。"}
        selected_ai = [item for item in selected if is_ai_protected_path(item.get("path"), [])]
        if selected_ai and not allow_ai:
            return {"ok": False, "error": "AI 缓存、模型或项目文件需要在界面中明确勾选并确认后才能清理。"}
        return original_cleanup(self, identifiers)

    api_class.storage_scan_status = storage_scan_status
    api_class.storage_cleanup = storage_cleanup
    api_class.storage_ai_exclusions_list = storage_ai_exclusions_list
    api_class.storage_ai_exclusions_add = storage_ai_exclusions_add
    api_class.storage_ai_exclusions_remove = storage_ai_exclusions_remove
    api_class._lx_storage_safety_installed = True
    return True
