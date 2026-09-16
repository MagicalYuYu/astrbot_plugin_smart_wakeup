"""Web API 模块 — v1.9.0 配置面板后端

为插件自定义 Web 配置页面（pages/config/）提供 REST API 支持。

注册的路由（前缀：/astrbot_plugin_smart_wakeup/）：
- GET  /config                获取完整配置
- POST /config                保存完整配置
- GET  /config/schema         获取配置 schema（含默认值/类型/hint/分类）
- GET  /config/status         获取运行时状态（精力/心流/buffer/退让/统计）
- POST /config/reload         保存当前状态并提示重载
- GET  /config/presets        获取预设列表（内置 + 用户自定义）
- POST /config/preset/<name>  应用指定预设
- GET  /config/logs           获取最近 N 条日志
- POST /config/test_speak     触发测试发言（可选，依赖插件方法）
- POST /config/reset_state    重置指定群的状态

设计要点：
1. 路由前缀使用 PLUGIN_NAME 常量（与 metadata.yaml name 字段一致）
2. 所有 view_handler 为 async 函数，返回 Quart jsonify(...) Response
3. v1.9.0 配置源：plugin.config（AstrBotConfig 实例，框架管理）
4. 加载优先级：plugin.config > _conf_schema.json 默认值（回退用）
5. 错误处理：所有异常捕获并返回 JSON 错误响应，记录到 logger

依赖：
- quart（AstrBot 框架依赖）
- AstrBot Context.register_web_api（core/star/context.py L515）
"""

import os
import json
import time
import logging
import traceback
import inspect
from collections import deque
from typing import Any, Optional

from quart import jsonify, request

logger = logging.getLogger("astrbot")

# 插件名（与 metadata.yaml name 字段一致，用于路由前缀）
PLUGIN_NAME = "astrbot_plugin_smart_wakeup"

# 配置文件版本
CONFIG_VERSION = "1.9.7"


# ============================================================================
# Schema 与预设路径工具
# ============================================================================

def _get_schema_path(plugin_root: str) -> str:
    """获取 _conf_schema.json 路径"""
    return os.path.join(plugin_root, "_conf_schema.json")


def _get_user_presets_path(plugin_root: str) -> str:
    """获取用户自定义预设文件路径"""
    return os.path.join(plugin_root, "data", "presets.json")


def _extract_defaults_from_schema(plugin_root: str) -> dict:
    """从 _conf_schema.json 提取默认值

    Args:
        plugin_root: 插件根目录路径

    Returns:
        配置字典，结构为 {category_name: {item_name: default_value, ...}, ...}
    """
    schema_path = _get_schema_path(plugin_root)
    if not os.path.exists(schema_path):
        return {}

    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except Exception as e:
        logger.warning(f"[smart_wakeup] 加载 _conf_schema.json 失败: {e}")
        return {}

    config = {}
    for category_name, category_def in schema.items():
        if not isinstance(category_def, dict):
            continue
        if category_def.get("type") != "object":
            continue
        items = category_def.get("items", {})
        if not isinstance(items, dict):
            continue
        category_config = {}
        for item_name, item_def in items.items():
            if not isinstance(item_def, dict):
                continue
            category_config[item_name] = item_def.get("default")
        config[category_name] = category_config
    return config


# ============================================================================
# 内置预设
# ============================================================================

BUILTIN_PRESETS = {
    "default": {
        "name": "默认配置",
        "description": "恢复到 schema 默认值，适用于通用场景",
        "config": None,  # None 表示使用 schema 默认值并保存到框架配置文件
    },
    "active_group": {
        "name": "活跃群配置",
        "description": "提高发言概率，降低冷却时间，适用于高活跃度群聊",
        "config": {
            "proactive_speak": {
                "proactive_enabled": True,
                "proactive_probability": 0.8,
                "proactive_cooldown": 1200,
                "proactive_daily_limit": 30,
            },
            "flow": {
                "flow_bystander_prob": 0.15,
                "flow_attentive_prob": 0.30,
                "flow_flow_prob": 0.50,
            },
            "energy": {
                "energy_decay_rate": 0.10,
                "energy_recovery_rate": 0.03,
            },
        },
    },
    "quiet_group": {
        "name": "安静群配置",
        "description": "降低发言概率，提高冷却时间，适用于低活跃度群聊",
        "config": {
            "proactive_speak": {
                "proactive_enabled": True,
                "proactive_probability": 0.4,
                "proactive_cooldown": 3600,
                "proactive_daily_limit": 10,
            },
            "flow": {
                "flow_bystander_prob": 0.05,
                "flow_attentive_prob": 0.15,
                "flow_flow_prob": 0.30,
            },
            "energy": {
                "energy_decay_rate": 0.20,
                "energy_recovery_rate": 0.01,
            },
        },
    },
}


def _load_all_presets(plugin_root: str) -> dict:
    """加载所有预设（内置 + 用户自定义）

    Args:
        plugin_root: 插件根目录路径

    Returns:
        预设字典 {preset_id: {name, description, config}, ...}
    """
    presets = dict(BUILTIN_PRESETS)
    user_presets_path = _get_user_presets_path(plugin_root)
    if os.path.exists(user_presets_path):
        try:
            with open(user_presets_path, "r", encoding="utf-8") as f:
                presets.update(json.load(f))
        except Exception as e:
            logger.warning(f"[smart_wakeup] 加载用户预设失败: {e}")
    return presets


# ============================================================================
# 配置验证工具（v1.9.7 新增 M4）
# ============================================================================

def _validate_config_against_schema(plugin_root: str, config: dict) -> list:
    """基于 _conf_schema.json 验证配置项类型

    v1.9.7 新增 M4：POST /config 保存前验证配置项类型，
    防止前端传入错误类型的值（如 string 传给 int 字段）导致插件运行异常。

    验证规则：
    - int/float: 必须是 number（int 或 float），不接受 bool（Python 中 bool 是 int 子类）
    - bool: 必须是 bool
    - string: 必须是 str
    - list/template_list/object: 不做类型检查（结构复杂，前端已控制）
    - 数值范围：int/float 类型检查 min/max（若 schema 中定义了 min/max）

    Args:
        plugin_root: 插件根目录路径
        config: 待验证的配置字典

    Returns:
        错误列表，空列表表示验证通过。每个错误格式：
        "category.key: 期望 <type>, 实际 <actual_type>"
    """
    errors = []
    schema_path = _get_schema_path(plugin_root)
    if not os.path.exists(schema_path):
        return errors  # schema 不存在时跳过验证

    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except Exception:
        return errors  # schema 加载失败时跳过验证

    for category, items in config.items():
        if not isinstance(items, dict):
            continue
        cat_schema = schema.get(category, {})
        if not isinstance(cat_schema, dict):
            continue
        cat_items = cat_schema.get("items", {})
        if not isinstance(cat_items, dict):
            continue

        for key, value in items.items():
            item_schema = cat_items.get(key)
            if not item_schema or not isinstance(item_schema, dict):
                continue  # schema 中未定义的项跳过

            expected_type = item_schema.get("type", "")
            if not expected_type:
                continue

            # bool 检查必须在 int/float 之前（Python 中 bool 是 int 子类）
            if expected_type == "bool":
                if not isinstance(value, bool):
                    errors.append(f"{category}.{key}: 期望 bool, 实际 {type(value).__name__}")
            elif expected_type == "int":
                if isinstance(value, bool) or not isinstance(value, int):
                    errors.append(f"{category}.{key}: 期望 int, 实际 {type(value).__name__}")
                elif "min" in item_schema and value < item_schema["min"]:
                    errors.append(f"{category}.{key}: 值 {value} 小于最小值 {item_schema['min']}")
                elif "max" in item_schema and value > item_schema["max"]:
                    errors.append(f"{category}.{key}: 值 {value} 大于最大值 {item_schema['max']}")
            elif expected_type == "float":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(f"{category}.{key}: 期望 float, 实际 {type(value).__name__}")
                elif "min" in item_schema and value < item_schema["min"]:
                    errors.append(f"{category}.{key}: 值 {value} 小于最小值 {item_schema['min']}")
                elif "max" in item_schema and value > item_schema["max"]:
                    errors.append(f"{category}.{key}: 值 {value} 大于最大值 {item_schema['max']}")
            elif expected_type == "string":
                if not isinstance(value, str):
                    errors.append(f"{category}.{key}: 期望 string, 实际 {type(value).__name__}")
            # list/template_list/object 类型不做严格检查

    return errors


# ============================================================================
# 路由注册主函数
# ============================================================================

def register_web_apis(plugin, context):
    """向 AstrBot 注册所有 Web API 路由

    在插件 __init__ 末尾调用此函数：
        from modules.web_api import register_web_apis
        register_web_apis(self, context)

    Args:
        plugin: LingxiPlugin 实例（self）
        context: AstrBot Context 对象
    """
    # 获取插件根目录路径（main.py 所在目录）
    plugin_root = os.path.dirname(os.path.abspath(inspect.getfile(type(plugin))))

    prefix = f"/{PLUGIN_NAME}"

    # ===== 配置管理 =====

    async def get_config():
        """GET /config - 获取完整配置

        v1.9.0 修复：直接从 plugin.config（AstrBotConfig 实例）读取框架管理的配置，
        而非 full_config.json，确保与 AstrBot 默认面板配置完全一致。
        """
        try:
            config_data = {}
            for key, val in dict(plugin.config).items():
                if key.startswith("_"):
                    continue  # 跳过框架内部字段
                config_data[key] = val
            return jsonify({"ok": True, "data": config_data})
        except Exception as e:
            logger.error(f"[smart_wakeup] GET /config 失败: {e}\n{traceback.format_exc()}")
            return jsonify({"ok": False, "error": str(e)}), 500

    async def post_config():
        """POST /config - 保存完整配置

        v1.9.0 修复：通过 plugin.config.save_config() 保存到框架配置文件
        （data/config/astrbot_plugin_smart_wakeup_config.json），
        而非 full_config.json，确保与 AstrBot 默认面板配置一致。
        """
        try:
            new_config = await request.get_json(force=True, silent=False)
            if not isinstance(new_config, dict):
                return jsonify({"ok": False, "error": "配置必须是 JSON 对象"}), 400

            # 过滤掉前端特有字段（如 _meta），只保留配置分类
            clean_config = {}
            for key, val in new_config.items():
                if key.startswith("_"):
                    continue
                clean_config[key] = val

            # v1.9.7 新增 M4：基于 schema 的配置类型验证
            # 防止前端传入错误类型的值（如 string 传给 int 字段）导致插件运行异常
            validation_errors = _validate_config_against_schema(plugin_root, clean_config)
            if validation_errors:
                logger.warning(f"[smart_wakeup] 配置验证失败: {validation_errors}")
                return jsonify({
                    "ok": False,
                    "error": "配置验证失败",
                    "details": validation_errors
                }), 400

            # v1.9.6 修正：深度合并（category 级），避免前端发送部分配置时同级字段丢失
            # 原问题：save_config(clean_config) 的 self.update() 是浅合并，
            # 嵌套字典（如 proactive_speak）被整体替换，同级字段丢失。
            # 修复：与 post_preset 一致，先读取当前配置，再按 category 合并。
            current_config = {}
            for key, val in dict(plugin.config).items():
                if key.startswith("_"):
                    continue
                current_config[key] = val

            for category, items in clean_config.items():
                if category not in current_config:
                    current_config[category] = {}
                if isinstance(items, dict) and isinstance(current_config[category], dict):
                    current_config[category].update(items)
                else:
                    current_config[category] = items

            plugin.config.save_config(current_config)
            logger.info(f"[smart_wakeup] 配置已通过 Web API 保存到框架配置文件")
            return jsonify({"ok": True, "message": "配置已保存到框架配置文件"})
        except Exception as e:
            logger.error(f"[smart_wakeup] POST /config 失败: {e}\n{traceback.format_exc()}")
            return jsonify({"ok": False, "error": str(e)}), 500

    async def get_schema():
        """GET /config/schema - 获取配置 schema

        v1.9.7 修复 M8：在 schema 数据中注入 version 字段，
        供前端 schemaVersion 计算属性读取（原仅在外层返回，前端无法获取）。
        """
        try:
            schema_path = _get_schema_path(plugin_root)
            if not os.path.exists(schema_path):
                return jsonify({"ok": False, "error": "schema 文件不存在"}), 404
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
            # v1.9.7：注入版本号到 schema 数据，前端 schema.value.version 可读取
            schema["version"] = CONFIG_VERSION
            return jsonify({
                "ok": True,
                "data": schema,
                "version": CONFIG_VERSION,
            })
        except Exception as e:
            logger.error(f"[smart_wakeup] GET /config/schema 失败: {e}\n{traceback.format_exc()}")
            return jsonify({"ok": False, "error": str(e)}), 500

    async def get_status():
        """GET /config/status - 获取运行时状态"""
        try:
            status = _build_runtime_status(plugin)
            return jsonify({"ok": True, "data": status})
        except Exception as e:
            logger.error(f"[smart_wakeup] GET /config/status 失败: {e}\n{traceback.format_exc()}")
            return jsonify({"ok": False, "error": str(e)}), 500

    async def post_reload():
        """POST /config/reload - 保存状态并触发框架级热重载

        v1.9.0 修复：通过 context._star_manager.reload() 触发框架级热重载，
        确保配置变更立即生效（包括 __init__ 中缓存的配置）。
        """
        try:
            # 1. 调用插件的 _save_proactive_state 保存持久化状态
            if hasattr(plugin, "_save_proactive_state"):
                plugin._save_proactive_state()
                logger.info("[smart_wakeup] 状态已通过 Web API 保存")

            # 2. v1.9.0 修复：触发框架级热重载，让配置变更立即生效
            reloaded = False
            reload_error = None
            try:
                star_manager = getattr(context, "_star_manager", None)
                if star_manager and hasattr(star_manager, "reload"):
                    success, err_msg = await star_manager.reload(PLUGIN_NAME)
                    reloaded = success
                    if not success and err_msg:
                        reload_error = err_msg
                        logger.warning(f"[smart_wakeup] 框架热重载返回失败: {err_msg}")
                    else:
                        logger.info(f"[smart_wakeup] 框架热重载成功")
            except Exception as e:
                reload_error = str(e)
                logger.warning(f"[smart_wakeup] 框架热重载异常: {e}")

            return jsonify({
                "ok": True,
                "message": "状态已保存" + ("，框架热重载已触发" if reloaded else "，请通过 AstrBot Dashboard 手动重载插件"),
                "reloaded": reloaded,
                "reload_error": reload_error,
            })
        except Exception as e:
            logger.error(f"[smart_wakeup] POST /config/reload 失败: {e}\n{traceback.format_exc()}")
            return jsonify({"ok": False, "error": str(e)}), 500

    # ===== 预设管理 =====

    async def get_presets():
        """GET /config/presets - 获取预设列表"""
        try:
            presets = _load_all_presets(plugin_root)
            preset_list = [
                {
                    "id": pid,
                    "name": p.get("name", pid),
                    "description": p.get("description", ""),
                    "is_builtin": pid in BUILTIN_PRESETS,
                }
                for pid, p in presets.items()
            ]
            return jsonify({"ok": True, "data": preset_list})
        except Exception as e:
            logger.error(f"[smart_wakeup] GET /config/presets 失败: {e}\n{traceback.format_exc()}")
            return jsonify({"ok": False, "error": str(e)}), 500

    async def post_preset(name):
        """POST /config/preset/<name> - 应用指定预设

        v1.9.0 修复：通过 plugin.config.save_config() 保存到框架配置文件。
        """
        try:
            presets = _load_all_presets(plugin_root)
            if name not in presets:
                return jsonify({"ok": False, "error": f"预设 '{name}' 不存在"}), 404

            preset = presets[name]
            preset_config = preset.get("config")

            if preset_config is None:
                # "default" 预设：从 _conf_schema.json 提取默认值并保存到框架配置
                default_config = _extract_defaults_from_schema(plugin_root)
                plugin.config.save_config(default_config)
                logger.info(f"[smart_wakeup] 已应用预设 '{name}'（恢复默认值）")
                return jsonify({"ok": True, "message": f"已应用预设 '{name}'（恢复默认值）"})

            # 应用预设配置（深度合并到当前配置）
            # v1.9.0 修复：从 plugin.config 读取当前配置，而非 full_config.json
            current_config = {}
            for key, val in dict(plugin.config).items():
                if key.startswith("_"):
                    continue
                current_config[key] = val

            for category, items in preset_config.items():
                if category not in current_config:
                    current_config[category] = {}
                if isinstance(items, dict):
                    current_config[category].update(items)

            # v1.9.0 修复：保存到框架配置文件，而非 full_config.json
            plugin.config.save_config(current_config)
            logger.info(f"[smart_wakeup] 已应用预设 '{name}'")
            return jsonify({"ok": True, "message": f"已应用预设 '{name}'"})
        except Exception as e:
            logger.error(f"[smart_wakeup] POST /config/preset/<name> 失败: {e}\n{traceback.format_exc()}")
            return jsonify({"ok": False, "error": str(e)}), 500

    # ===== 日志查看 =====

    async def get_logs():
        """GET /config/logs - 获取最近 N 条日志

        Query params:
            limit: 返回行数（1-1000，默认 100）
            level: 日志级别过滤（DEBUG/INFO/WARNING/ERROR，可选）
        """
        try:
            limit_str = request.args.get("limit", "100")
            try:
                limit = max(1, min(1000, int(limit_str)))
            except ValueError:
                limit = 100

            level = request.args.get("level", "").upper()

            # 尝试多个可能的日志路径
            possible_paths = [
                os.path.join(plugin_root, "data", "plugin.log"),
                os.path.join(plugin_root, "logs", "plugin.log"),
            ]
            log_path = None
            for path in possible_paths:
                if os.path.exists(path):
                    log_path = path
                    break

            if not log_path:
                return jsonify({
                    "ok": True,
                    "data": [],
                    "count": 0,
                    "message": "无插件日志文件",
                })

            # 使用 deque 保留最后 N 行（避免大文件内存爆炸）
            logs = []
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = deque(f, maxlen=limit)
                logs = [line.rstrip("\n") for line in lines]
            except Exception as e:
                logger.warning(f"[smart_wakeup] 读取插件日志失败: {e}")

            # 按级别过滤（简单字符串匹配）
            if level:
                logs = [l for l in logs if f"[{level}]" in l.upper() or f" {level} " in l.upper()]

            return jsonify({
                "ok": True,
                "data": logs,
                "count": len(logs),
                "path": log_path,
            })
        except Exception as e:
            logger.error(f"[smart_wakeup] GET /config/logs 失败: {e}\n{traceback.format_exc()}")
            return jsonify({"ok": False, "error": str(e)}), 500

    # ===== 测试发言 =====

    async def post_test_speak():
        """POST /config/test_speak - 触发测试发言

        Body:
            group_id: str  目标群 ID（必填）
            topic: str     话题（可选）
        """
        try:
            body = await request.get_json(force=True, silent=True) or {}
            group_id = body.get("group_id")
            topic = body.get("topic", "")

            if not group_id:
                return jsonify({"ok": False, "error": "group_id 必填"}), 400

            # 检查插件是否实现了测试发言接口
            if not hasattr(plugin, "_trigger_test_speak"):
                return jsonify({
                    "ok": False,
                    "error": "插件未实现 _trigger_test_speak 方法",
                }), 501

            result = await plugin._trigger_test_speak(str(group_id), topic)
            return jsonify({"ok": True, "data": result})
        except Exception as e:
            logger.error(f"[smart_wakeup] POST /config/test_speak 失败: {e}\n{traceback.format_exc()}")
            return jsonify({"ok": False, "error": str(e)}), 500

    # ===== 状态重置 =====

    async def post_reset_state():
        """POST /config/reset_state - 重置指定群的状态

        Body:
            group_id: str      目标群 ID（必填）
            types: list[str]   重置类型（可选，默认 ["all"]）
                可选值：all/energy/flow/buffer/proactive
        """
        try:
            body = await request.get_json(force=True, silent=True) or {}
            group_id = body.get("group_id")
            reset_types = body.get("types", ["all"])

            if not group_id:
                return jsonify({"ok": False, "error": "group_id 必填"}), 400

            if not isinstance(reset_types, list):
                return jsonify({"ok": False, "error": "types 必须是列表"}), 400

            reset_result = _reset_group_state(plugin, str(group_id), reset_types)
            return jsonify({"ok": True, "data": reset_result})
        except Exception as e:
            logger.error(f"[smart_wakeup] POST /config/reset_state 失败: {e}\n{traceback.format_exc()}")
            return jsonify({"ok": False, "error": str(e)}), 500

    # ===== 注册所有路由 =====

    routes = [
        # 配置管理
        (f"{prefix}/config", get_config, ["GET"], "获取完整配置"),
        (f"{prefix}/config", post_config, ["POST"], "保存完整配置"),
        (f"{prefix}/config/schema", get_schema, ["GET"], "获取配置 schema"),
        (f"{prefix}/config/status", get_status, ["GET"], "获取运行时状态"),
        (f"{prefix}/config/reload", post_reload, ["POST"], "保存状态并提示重载"),
        # 预设管理
        (f"{prefix}/config/presets", get_presets, ["GET"], "获取预设列表"),
        (f"{prefix}/config/preset/<name>", post_preset, ["POST"], "应用指定预设"),
        # 日志与调试
        (f"{prefix}/config/logs", get_logs, ["GET"], "获取最近日志"),
        (f"{prefix}/config/test_speak", post_test_speak, ["POST"], "触发测试发言"),
        (f"{prefix}/config/reset_state", post_reset_state, ["POST"], "重置指定群状态"),
    ]

    for route, handler, methods, desc in routes:
        context.register_web_api(route, handler, methods, desc)
        logger.info(f"[smart_wakeup] 已注册 Web API: {','.join(methods)} {route} - {desc}")


# ============================================================================
# 辅助函数：构建运行时状态
# ============================================================================

def _build_runtime_status(plugin) -> dict:
    """构建运行时状态数据

    汇总插件内存中的所有运行时状态，供 GET /config/status 端点返回。

    Args:
        plugin: LingxiPlugin 实例

    Returns:
        状态字典，结构见 v1.9.0 规划文档 5.4 节
    """
    groups_status = {}

    # 收集所有 group_id（取各状态字典的并集）
    state_dict_names = [
        "_msg_buffer",
        "_energy_states",
        "_flow_states",
        "_proactive_response_tracker",
        "_proactive_daily_count",
        "_proactive_last_speak",
        "_proactive_states",
        "_proactive_retreat",
    ]
    all_group_ids = set()
    for state_name in state_dict_names:
        state = getattr(plugin, state_name, {})
        if isinstance(state, dict):
            all_group_ids.update(state.keys())

    today_str = time.strftime("%Y-%m-%d")

    for group_id in all_group_ids:
        # 精力值
        energy_state = plugin._energy_states.get(group_id) if hasattr(plugin, "_energy_states") else None
        energy_value = getattr(energy_state, "energy", 1.0) if energy_state else 1.0

        # 心流状态
        flow_state = plugin._flow_states.get(group_id) if hasattr(plugin, "_flow_states") else None
        flow_state_enum = getattr(flow_state, "state", None) if flow_state else None
        flow_state_name = getattr(flow_state_enum, "name", "UNKNOWN") if flow_state_enum else "UNKNOWN"
        engagement = getattr(flow_state, "engagement", 0.0) if flow_state else 0.0
        conversation_turns = getattr(flow_state, "conversation_turns", 0) if flow_state else 0

        # 消息缓冲区
        msg_buffer = plugin._msg_buffer.get(group_id, []) if hasattr(plugin, "_msg_buffer") else []
        buffer_count = len(msg_buffer) if hasattr(msg_buffer, "__len__") else 0
        buffer_recent = []
        if hasattr(msg_buffer, "__iter__"):
            for msg in list(msg_buffer)[-5:]:  # 最近 5 条
                if isinstance(msg, (list, tuple)) and len(msg) >= 2:
                    buffer_recent.append({
                        "sender": str(msg[0]) if msg[0] else "",
                        "text": str(msg[1])[:100] if msg[1] else "",
                        "timestamp": msg[2] if len(msg) > 2 else 0,
                    })

        # 退让状态
        response_tracker = {}
        if hasattr(plugin, "_proactive_response_tracker"):
            response_tracker = plugin._proactive_response_tracker.get(group_id, {})
        retreat = {}
        if hasattr(plugin, "_proactive_retreat"):
            retreat = plugin._proactive_retreat.get(group_id, {})

        # 今日发言次数
        daily_count_dict = {}
        if hasattr(plugin, "_proactive_daily_count"):
            daily_count_dict = plugin._proactive_daily_count.get(group_id, {})
        today_count = daily_count_dict.get(today_str, 0)

        # 上次发言时间
        last_speak_ts = 0.0
        if hasattr(plugin, "_proactive_last_speak"):
            last_speak_ts = plugin._proactive_last_speak.get(group_id, 0.0)

        # AIF 状态
        aif_state = "PASSIVE_MONITORING"
        if hasattr(plugin, "_proactive_states"):
            aif_state = plugin._proactive_states.get(group_id, "PASSIVE_MONITORING")

        # 组装群组状态
        groups_status[str(group_id)] = {
            "energy": round(energy_value, 4),
            "flow_state": flow_state_name,
            "engagement": round(engagement, 4),
            "conversation_turns": conversation_turns,
            "buffer_count": buffer_count,
            "buffer_recent": buffer_recent,
            "last_speak": last_speak_ts,
            "consecutive_no_response": response_tracker.get("consecutive_no_response_count", 0),
            "cooldown_multiplier": response_tracker.get("cooldown_multiplier", 1.0),
            "today_speak_count": today_count,
            "aif_state": aif_state,
            "retreat_until": retreat.get("until", 0),
            "retreat_reason": retreat.get("reason", ""),
        }

    # 全局统计
    stats = getattr(plugin, "_stats", {})
    proactive_stats = getattr(plugin, "_proactive_stats", {})

    return {
        "version": CONFIG_VERSION,
        "timestamp": time.time(),
        "plugin_uptime": time.time() - stats.get("plugin_start_time", time.time()),
        "groups": groups_status,
        "global_stats": {
            "total_messages_recorded": stats.get("total_messages_recorded", 0),
            "total_wakeups": stats.get("total_wakeups", 0),
            "name_trigger_wakeups": stats.get("name_trigger_wakeups", 0),
            "probability_wakeups": stats.get("probability_wakeups", 0),
            "rescue_wakeups": stats.get("rescue_wakeups", 0),
            "total_tokens": stats.get("total_tokens", 0),
            "total_prompt_tokens": stats.get("total_prompt_tokens", 0),
            "total_completion_tokens": stats.get("total_completion_tokens", 0),
            "llm_call_count": stats.get("llm_call_count", 0),
            "plugin_start_time": stats.get("plugin_start_time", 0),
            "proactive_total_attempts": proactive_stats.get("total_attempts", 0),
            "proactive_total_success": proactive_stats.get("total_success", 0),
            "proactive_total_skip": proactive_stats.get("total_skip", 0),
            "proactive_total_retreats": proactive_stats.get("total_retreats", 0),
        },
        "proactive": {
            "is_running": hasattr(plugin, "_proactive_task") and plugin._proactive_task is not None,
            "enabled": getattr(plugin, "_proactive_enabled", False),
            "check_interval": getattr(plugin, "_proactive_check_interval", 600),
            "cooldown": getattr(plugin, "_proactive_cooldown", 1800),
            "daily_limit": getattr(plugin, "_proactive_daily_limit", 20),
        },
    }


# ============================================================================
# 辅助函数：重置群组状态
# ============================================================================

def _reset_group_state(plugin, group_id: str, reset_types: list) -> dict:
    """重置指定群的状态

    Args:
        plugin: LingxiPlugin 实例
        group_id: 群 ID
        reset_types: 重置类型列表，可选值 all/energy/flow/buffer/proactive

    Returns:
        重置结果字典 {group_id, reset: [...], saved: bool}
    """
    reset_result = {"group_id": group_id, "reset": [], "saved": False}
    reset_all = "all" in reset_types

    # 重置精力
    if reset_all or "energy" in reset_types:
        if hasattr(plugin, "_energy_states") and group_id in plugin._energy_states:
            try:
                energy_cls = type(plugin._energy_states[group_id])
                plugin._energy_states[group_id] = energy_cls()
                reset_result["reset"].append("energy")
            except Exception as e:
                logger.warning(f"[smart_wakeup] 重置 energy 失败: {e}")

    # 重置心流
    if reset_all or "flow" in reset_types:
        if hasattr(plugin, "_flow_states") and group_id in plugin._flow_states:
            try:
                flow_cls = type(plugin._flow_states[group_id])
                plugin._flow_states[group_id] = flow_cls()
                reset_result["reset"].append("flow")
            except Exception as e:
                logger.warning(f"[smart_wakeup] 重置 flow 失败: {e}")

    # 清空消息缓冲区
    if reset_all or "buffer" in reset_types:
        if hasattr(plugin, "_msg_buffer") and group_id in plugin._msg_buffer:
            try:
                plugin._msg_buffer[group_id].clear()
                reset_result["reset"].append("buffer")
            except Exception as e:
                logger.warning(f"[smart_wakeup] 重置 buffer 失败: {e}")

    # 重置主动发言相关状态
    if reset_all or "proactive" in reset_types:
        if hasattr(plugin, "_proactive_response_tracker") and group_id in plugin._proactive_response_tracker:
            try:
                plugin._proactive_response_tracker[group_id] = {
                    "speak_time": 0,
                    "checked": False,
                    "cooldown_multiplier": 1.0,
                    "consecutive_no_response_count": 0,
                }
                reset_result["reset"].append("proactive_tracker")
            except Exception as e:
                logger.warning(f"[smart_wakeup] 重置 proactive_tracker 失败: {e}")

        if hasattr(plugin, "_proactive_retreat") and group_id in plugin._proactive_retreat:
            try:
                del plugin._proactive_retreat[group_id]
                reset_result["reset"].append("retreat")
            except Exception as e:
                logger.warning(f"[smart_wakeup] 重置 retreat 失败: {e}")

        if hasattr(plugin, "_proactive_recent_openers") and group_id in plugin._proactive_recent_openers:
            try:
                plugin._proactive_recent_openers[group_id].clear()
                reset_result["reset"].append("openers")
            except Exception as e:
                logger.warning(f"[smart_wakeup] 重置 openers 失败: {e}")

        if hasattr(plugin, "_proactive_topic_history") and group_id in plugin._proactive_topic_history:
            try:
                plugin._proactive_topic_history[group_id].clear()
                reset_result["reset"].append("topic_history")
            except Exception as e:
                logger.warning(f"[smart_wakeup] 重置 topic_history 失败: {e}")

    # 保存持久化状态
    if hasattr(plugin, "_save_proactive_state"):
        try:
            plugin._save_proactive_state()
            reset_result["saved"] = True
        except Exception as e:
            reset_result["saved"] = False
            reset_result["save_error"] = str(e)
            logger.warning(f"[smart_wakeup] 保存状态失败: {e}")

    return reset_result
