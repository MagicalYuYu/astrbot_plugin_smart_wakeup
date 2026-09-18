/* ============================================================================
 * 灵犀配置面板 · app.js
 * ----------------------------------------------------------------------------
 * 框架：Vue 3 Composition API（不使用 Options API）
 * 通信：桥接 SDK（window.AstrBotPluginPage，由 AstrBot 自动注入，无需手动引入）
 * 路由：Hash routing（#basic / #proactive 等）
 *
 * 主要职责：
 * 1. 调用 AstrBotPluginPage.ready() 等待桥接 SDK 就绪
 * 2. 拉取 schema（/config/schema）和当前配置（/config）
 * 3. 渲染 14 分类共 143 项配置（按 7 大类侧边栏分组）
 * 4. 搜索过滤、简单/高级模式切换
 * 5. 保存（/config POST）、重置、导入、导出
 * 6. 预设管理（/config/presets + /config/preset/<name>）
 * 7. 实时状态面板（每 5 秒拉取 /config/status）
 * 8. 调试控制台（/config/logs + /config/test_speak + /config/reset_state）
 *
 * 错误处理：所有 API 调用均 try/catch，错误通过 UI 顶部消息条显示
 * 加载状态：所有异步操作均设置 loading.* 状态字段
 * ============================================================================ */

const { createApp, reactive, ref, computed, onMounted, onUnmounted } = Vue;

/* ============================================================================
 * 7 大类侧边栏映射（基于 14 个 schema 分类合并）
 * 顺序与任务规划文档 4.3 节一致
 * ============================================================================ */
const SIDEBAR_GROUPS = [
    { id: 'basic',     icon: '📋', label: '基础设置',   categories: ['basic', 'group_filter'] },
    { id: 'proactive', icon: '⚡', label: '主动发言',   categories: ['proactive_speak', 'rescue'] },
    { id: 'fetcher',   icon: '📰', label: '资讯 Fetcher', categories: ['fetcher'] },
    { id: 'flow',      icon: '🧠', label: '心流状态机', categories: ['flow', 'energy'] },
    { id: 'message',   icon: '💬', label: '消息处理',   categories: ['debounce', 'splitter', 'light_response', 'reply_suppression', 'filter_settings'] },
    { id: 'image',     icon: '🖼️', label: '图片识别',   categories: ['image_context'] },
    { id: 'advanced',  icon: '🔧', label: '高级调优',   categories: ['advanced'] }
];

/* ============================================================================
 * 内置预设兜底（API 不可用时使用，确保 UI 不空白）
 * ============================================================================ */
const FALLBACK_PRESETS = [
    { id: 'default',      name: '默认配置',   description: '平衡的默认值，适合大多数场景', is_builtin: true },
    { id: 'active_group', name: '活跃群配置', description: '提高发言概率，降低冷却时间', is_builtin: true },
    { id: 'quiet_group',  name: '安静群配置', description: '降低发言概率，提高冷却时间', is_builtin: true }
];

createApp({
    setup() {
        // ====================================================================
        // 响应式状态
        // ====================================================================

        // 加载状态（每个异步操作独立标记，UI 可精准反馈）
        const loading = reactive({
            init: true,        // 首次初始化遮罩
            schema: false,     // 加载 schema
            config: false,     // 加载/保存配置
            status: false,     // 加载运行时状态
            presets: false,    // 加载预设
            logs: false,       // 加载日志
            testSpeak: false,  // 触发测试发言
            resetState: false  // 重置状态
        });

        // 初始化遮罩文字（用于细化加载阶段反馈）
        const loadingText = ref('');

        // 顶部消息条（type: info/success/warning/error）
        const message = reactive({ text: '', type: 'info', timer: null });

        // schema 数据（{category: {description, hint, items: {...}}, version: '...'}）
        const schema = ref({});

        // 当前配置（双向绑定，用户编辑后实时变化）
        const config = reactive({});

        // 上次保存的配置快照（用于"重置"和"检测修改"）
        const savedConfig = ref({});

        // 运行时状态（{uptime, version, groups: {group_id: {...}}}）
        const status = reactive({ groups: {} });

        // 预设列表
        const presets = ref([]);

        // 调试控制台日志
        const logs = ref([]);

        // UI 状态
        const searchQuery = ref('');
        const activeGroup = ref('basic');
        const advancedMode = ref(false);
        const panels = reactive({ status: false, debug: false });
        const dropdowns = reactive({ presets: false });
        const autoRefreshStatus = ref(true);

        // 分区折叠状态（localStorage 持久化）
        const collapsedSections = ref({});
        try {
            const saved = localStorage.getItem('sw_collapsed_sections');
            if (saved) collapsedSections.value = JSON.parse(saved);
        } catch (e) {
            console.warn('[smart_wakeup] 读取折叠状态失败:', e);
        }

        // 调试控制台状态
        const debugTab = ref('logs');
        const logFilter = ref('');
        const logLimit = ref(100);
        const testSpeakForm = reactive({ group_id: '', topic: '' });
        const resetStateForm = reactive({ group_id: '', reset_types: [] });
        const testSpeakResult = ref('');
        const resetStateResult = ref('');

        // 定时器和防抖
        let statusTimer = null;
        let searchDebounceTimer = null;
        const committedSearchQuery = ref('');
        // 定时器统一管理（用于 onUnmounted 清理）
        const pendingTimers = [];

        // ====================================================================
        // Computed 计算属性
        // ====================================================================

        // Schema 版本（兜底 2.0.2）
        const schemaVersion = computed(() => schema.value.version || '2.0.2');

        // 总配置项数
        const totalItems = computed(() => {
            let count = 0;
            Object.keys(schema.value || {}).forEach(cat => {
                if (cat === 'version') return;
                const catSchema = schema.value[cat];
                if (catSchema && catSchema.items && typeof catSchema.items === 'object') {
                    count += Object.keys(catSchema.items).length;
                }
            });
            return count;
        });

        // 总分类数
        const totalCategories = computed(() => {
            return Object.keys(schema.value || {}).filter(k =>
                k !== 'version' && schema.value[k] && schema.value[k].items
            ).length;
        });

        // 侧边栏分组（带 itemCount）
        const sidebarGroups = computed(() => {
            return SIDEBAR_GROUPS.map(group => {
                const itemCount = group.categories.reduce((total, cat) => {
                    const catSchema = schema.value[cat];
                    return total + (catSchema && catSchema.items ? Object.keys(catSchema.items).length : 0);
                }, 0);
                return { ...group, itemCount };
            });
        });

        // 当前选中的分类组
        const currentGroup = computed(() =>
            SIDEBAR_GROUPS.find(g => g.id === activeGroup.value) || SIDEBAR_GROUPS[0]
        );
        const currentGroupIcon = computed(() => currentGroup.value.icon);
        const currentGroupLabel = computed(() => currentGroup.value.label);

        // 当前分类组合并 hint
        const currentGroupHint = computed(() => {
            const hints = currentGroup.value.categories
                .map(cat => schema.value[cat]?.hint)
                .filter(Boolean);
            return hints.join(' / ');
        });

        // 当前分类组配置项总数
        const currentGroupItemCount = computed(() => {
            return currentGroup.value.categories.reduce((total, cat) => {
                const catSchema = schema.value[cat];
                return total + (catSchema && catSchema.items ? Object.keys(catSchema.items).length : 0);
            }, 0);
        });

        // 当前分类组高级项数
        const currentGroupAdvancedCount = computed(() => {
            return allItems.value.filter(item =>
                currentGroup.value.categories.includes(item.category) && item.advanced
            ).length;
        });

        // 当前分类组已修改项数
        const currentGroupModifiedCount = computed(() => {
            return allItems.value.filter(item =>
                currentGroup.value.categories.includes(item.category) && isItemModified(item)
            ).length;
        });

        // 所有配置项扁平化（带 category/key/fullKey/templateKey）
        const allItems = computed(() => {
            const items = [];
            Object.keys(schema.value || {}).forEach(category => {
                if (category === 'version') return;
                const catSchema = schema.value[category];
                if (!catSchema || !catSchema.items) return;
                Object.keys(catSchema.items).forEach(key => {
                    const itemSchema = catSchema.items[key];
                    items.push({
                        ...itemSchema,
                        category,
                        key,
                        fullKey: `${category}.${key}`,
                        // template_list 类型提取首个模板 key（schema 中通常只有一个模板）
                        templateKey: itemSchema.type === 'template_list'
                            ? Object.keys(itemSchema.templates || {})[0]
                            : null
                    });
                });
            });
            return items;
        });

        // 过滤后的配置项（基于搜索 + 当前分类 + 高级模式）
        const filteredItems = computed(() => {
            let items = allItems.value;

            // 搜索模式：跨所有分类匹配（使用 committedSearchQuery 防抖后的值）
            if (committedSearchQuery.value.trim()) {
                const q = committedSearchQuery.value.toLowerCase().trim();
                items = items.filter(item => {
                    return (item.description || '').toLowerCase().includes(q) ||
                           (item.hint || '').toLowerCase().includes(q) ||
                           item.fullKey.toLowerCase().includes(q) ||
                           item.category.toLowerCase().includes(q);
                });
            } else {
                // 非搜索模式：仅显示当前分类组
                items = items.filter(item => currentGroup.value.categories.includes(item.category));
            }

            // 简单模式：过滤掉标记为 advanced 的项
            if (!advancedMode.value) {
                items = items.filter(item => !item.advanced);
            }

            return items;
        });

        // 按 schema 分类再次分组（用于分区渲染）
        const groupedFilteredItems = computed(() => {
            const groups = {};
            filteredItems.value.forEach(item => {
                if (!groups[item.category]) {
                    const catSchema = schema.value[item.category];
                    const rawDesc = catSchema?.description || item.category;
                    // v1.8.5 修复：从 description 提取首部 emoji 作为 icon，并从 description 中移除
                    // 避免 icon + description 同时渲染时 emoji 重复（如 "🧩 🧩 基础设置"）
                    // 格式约定："emoji 空格 文字" → icon="emoji", description="文字"
                    const iconMatch = rawDesc.match(/^(\S+)\s+(.*)$/);
                    groups[item.category] = {
                        category: item.category,
                        description: iconMatch ? iconMatch[2] : rawDesc,
                        icon: iconMatch ? iconMatch[1] : '📁',
                        hint: catSchema?.hint || '',
                        items: []
                    };
                }
                groups[item.category].items.push(item);
            });
            return Object.values(groups);
        });

        // 是否有未保存的修改
        const hasUnsavedChanges = computed(() => {
            return JSON.stringify(config) !== JSON.stringify(savedConfig.value);
        });

        // 群组 ID 列表（从 status.groups 提取，用于测试发言和状态重置下拉）
        const groupIds = computed(() => Object.keys(status.groups || {}));

        // 过滤后的日志
        const filteredLogs = computed(() => {
            if (!logFilter.value.trim()) return logs.value;
            const q = logFilter.value.toLowerCase().trim();
            return logs.value.filter(log => {
                const msg = log.message || log.msg || '';
                const level = (log.level || '').toLowerCase();
                return msg.toLowerCase().includes(q) || level.includes(q);
            });
        });

        // 是否有下拉打开（用于显示遮罩）
        const anyDropdownOpen = computed(() => Object.values(dropdowns).some(v => v));

        // ====================================================================
        // 工具方法
        // ====================================================================

        // 显示顶部消息条（自动消失，timeout=0 表示不自动消失）
        function showMessage(text, type = 'info', timeout = 5000) {
            if (message.timer) {
                clearTimeout(message.timer);
                message.timer = null;
            }
            message.text = text;
            message.type = type;
            if (timeout > 0) {
                message.timer = setTimeout(() => {
                    message.text = '';
                    message.timer = null;
                }, timeout);
            }
        }

        function clearMessage() {
            if (message.timer) {
                clearTimeout(message.timer);
                message.timer = null;
            }
            message.text = '';
        }

        // 统一 API 调用封装（带错误处理和数据格式归一化）
        // 后端返回格式可能是 {ok: true, data: ...} 或直接数据，这里统一提取 data
        async function callApi(method, endpoint, params = null, body = null) {
            try {
                let result;
                if (method === 'GET') {
                    result = await AstrBotPluginPage.apiGet(endpoint, params);
                } else if (method === 'POST') {
                    result = await AstrBotPluginPage.apiPost(endpoint, body);
                } else {
                    throw new Error(`不支持的 HTTP 方法: ${method}`);
                }
                // 归一化返回格式：{ok: false} → 抛出错误；{ok: true, data: X} → X；其他原样返回
                if (result && typeof result === 'object' && result.ok === false) {
                    const errMsg = result.error || result.message || result.msg || 'API 调用失败';
                    throw new Error(errMsg);
                }
                if (result && typeof result === 'object' && result.ok === true && result.data !== undefined) {
                    return result.data;
                }
                return result;
            } catch (err) {
                console.error(`[API ${method} ${endpoint}]`, err);
                throw err;
            }
        }

        // ====================================================================
        // 核心业务方法
        // ====================================================================

        // 应用初始化（入口函数）
        async function init() {
            loading.init = true;
            loadingText.value = '正在等待桥接 SDK 就绪...';

            try {
                // 1. 等待 AstrBot 桥接 SDK 注入并就绪
                // v1.8.5 修复：bridge-sdk.js 由 AstrBot 在 </body> 前注入，加载顺序晚于 app.js。
                // 而 createApp().mount() 同步触发 onMounted → init()，此时 bridge-sdk.js 尚未执行。
                // 因此不能用 typeof 立即判断，必须异步轮询等待 window.AstrBotPluginPage 被定义。
                const waitDeadline = Date.now() + 5000;  // 5 秒超时
                while (typeof window.AstrBotPluginPage === 'undefined') {
                    if (Date.now() > waitDeadline) {
                        throw new Error('桥接 SDK 等待超时（5秒内未检测到 window.AstrBotPluginPage）。请确认页面在 AstrBot 插件 iframe 中打开，且 bridge-sdk.js 资源可访问。');
                    }
                    await new Promise(r => setTimeout(r, 50));
                }
                await window.AstrBotPluginPage.ready();

                // 2. 并行拉取 schema 和当前配置（必须先于状态/预设加载，因为 UI 依赖 schema）
                loadingText.value = '正在加载配置 schema 和当前配置...';
                await Promise.all([loadSchema(), loadConfig()]);

                // 3. 加载运行时状态和预设列表（失败不阻塞主流程）
                loadingText.value = '正在加载运行时状态和预设...';
                await Promise.allSettled([loadStatus(), loadPresets()]);

                // 4. 解析 hash 路由（如 #proactive 自动切换到对应分类）
                parseHashRoute();

                // 5. 启动状态自动刷新定时器
                startStatusAutoRefresh();

                // 6. 后台静默加载日志（首次展开调试控制台时也会触发）
                loadLogs().catch(() => {});

                showMessage('配置面板加载完成', 'success', 3000);
            } catch (err) {
                console.error('初始化失败:', err);
                showMessage(`初始化失败: ${err.message}`, 'error', 0);
            } finally {
                loading.init = false;
                loadingText.value = '';
            }
        }

        // 加载配置 schema（/config/schema）
        async function loadSchema() {
            loading.schema = true;
            try {
                const result = await callApi('GET', 'config/schema');
                schema.value = result || {};
                // 为 config 预填充空对象，避免后续访问 config[cat][key] 时 undefined
                Object.keys(schema.value).forEach(cat => {
                    if (cat === 'version') return;
                    if (!config[cat]) config[cat] = {};
                });
            } catch (err) {
                showMessage(`加载 schema 失败: ${err.message}`, 'error');
                throw err;
            } finally {
                loading.schema = false;
            }
        }

        // 加载当前配置（/config）
        async function loadConfig() {
            loading.config = true;
            try {
                const result = await callApi('GET', 'config');
                // 将后端返回的配置合并到 reactive config
                Object.keys(result || {}).forEach(cat => {
                    if (cat === 'version' || cat === '_meta') return;
                    if (!config[cat]) config[cat] = {};
                    Object.keys(result[cat] || {}).forEach(key => {
                        let val = result[cat][key];
                        // v1.8.5 修复：后端可能返回 null 作为字段值（如 advanced 分类的
                        // group_overrides/user_prob_overrides/splitter_advanced）。当 schema 类型为
                        // object/template_list/list 时，null 会导致 v-model 访问 null[subKey] 抛出
                        // TypeError，整个 Vue 渲染失败页面空白。这里根据 schema 类型将 null 初始化
                        // 为合适的空容器，彻底解决 null 值导致的渲染问题。
                        if (val === null) {
                            const itemSchema = schema.value[cat]?.items?.[key];
                            if (itemSchema) {
                                if (itemSchema.type === 'object') {
                                    val = {};
                                } else if (itemSchema.type === 'template_list' || itemSchema.type === 'list') {
                                    val = [];
                                } else {
                                    // 标量类型：用 schema.default 兜底，否则空字符串
                                    val = itemSchema.default !== undefined ? itemSchema.default : '';
                                }
                            } else {
                                // schema 中无对应定义，保守初始化为空对象
                                val = {};
                            }
                        }
                        config[cat][key] = val;
                    });
                });
                // 保存快照（深拷贝，避免引用污染）
                savedConfig.value = JSON.parse(JSON.stringify(config));
            } catch (err) {
                showMessage(`加载配置失败: ${err.message}`, 'error');
                throw err;
            } finally {
                loading.config = false;
            }
        }

        // 保存配置（POST /config），保存前会做基本数据验证
        async function saveConfig() {
            loading.config = true;
            try {
                // 1. 数据验证（数值类型/必填项）
                const validationError = validateConfig();
                if (validationError) {
                    showMessage(`配置验证失败: ${validationError}`, 'error');
                    return;
                }

                // 2. 准备保存的数据（移除 _meta 等内部字段，做深拷贝避免后续修改影响）
                const configToSave = {};
                Object.keys(config).forEach(cat => {
                    if (cat === 'version' || cat === '_meta') return;
                    configToSave[cat] = JSON.parse(JSON.stringify(config[cat]));
                });

                // 3. 调用后端保存（v2.0.2：后端保存成功后会自行触发热重载）
                const saveResult = await callApi('POST', 'config', configToSave);

                // 4. 更新快照
                savedConfig.value = JSON.parse(JSON.stringify(config));

                // 5. 根据后端重载状态提示（重载失败必须让用户看见，不能只 console.warn）
                const reloaded = saveResult?.reloaded !== false;
                const backendMsg = saveResult?.message;
                if (reloaded) {
                    showMessage(backendMsg || '配置已保存并热生效', 'success');
                } else {
                    showMessage(backendMsg || '配置已保存，但热重载失败——请在 AstrBot 原生面板手动重载插件', 'warning');
                }
            } catch (err) {
                showMessage(`保存配置失败: ${err.message}`, 'error');
            } finally {
                loading.config = false;
            }
        }

        // 配置数据验证（保存前调用）
        // 返回：null=通过，字符串=错误描述
        function validateConfig() {
            for (const cat of Object.keys(schema.value || {})) {
                if (cat === 'version') continue;
                const catSchema = schema.value[cat];
                if (!catSchema || !catSchema.items) continue;

                for (const key of Object.keys(catSchema.items)) {
                    const itemSchema = catSchema.items[key];
                    const value = config[cat]?.[key];

                    // 数值类型验证
                    if (itemSchema.type === 'int' || itemSchema.type === 'float') {
                        if (value !== null && value !== undefined && value !== '') {
                            const num = Number(value);
                            if (isNaN(num)) {
                                return `${catSchema.description} > ${itemSchema.description}: 值 "${value}" 不是有效数字`;
                            }
                        }
                    }

                    // string 带选项时验证（如 reply_suppression_mode）
                    if (itemSchema.type === 'string' && itemSchema.options && itemSchema.options.length > 0) {
                        if (value && !itemSchema.options.includes(value)) {
                            // 允许但不强制（某些场景可能后端有扩展选项）
                        }
                    }
                }
            }
            return null;
        }

        // 重置当前所有修改（恢复到上次保存的快照）
        function resetConfig() {
            if (!hasUnsavedChanges.value) {
                showMessage('没有未保存的修改', 'info', 2000);
                return;
            }
            if (!confirm('确定放弃当前所有修改，恢复到上次保存的值吗？')) return;

            // 用深拷贝覆盖当前 config，保留响应式
            Object.keys(savedConfig.value).forEach(cat => {
                if (cat === 'version' || cat === '_meta') return;
                if (!config[cat]) config[cat] = {};
                const savedCat = savedConfig.value[cat] || {};
                // 清空当前分类所有 key
                Object.keys(config[cat]).forEach(k => delete config[cat][k]);
                // 重新填充
                Object.keys(savedCat).forEach(k => {
                    config[cat][k] = JSON.parse(JSON.stringify(savedCat[k]));
                });
            });
            showMessage('已恢复到上次保存的配置', 'info', 3000);
        }

        // 加载运行时状态（/config/status）
        async function loadStatus() {
            loading.status = true;
            try {
                const result = await callApi('GET', 'config/status');
                // 清空当前状态再填充（保留响应式）
                Object.keys(status).forEach(k => delete status[k]);
                if (result && typeof result === 'object') {
                    Object.keys(result).forEach(k => {
                        status[k] = result[k];
                    });
                }
                // 确保 groups 字段始终存在
                if (!status.groups) status.groups = {};
            } catch (err) {
                console.warn('加载状态失败:', err);
                // 静默失败，不打扰用户（状态面板非关键功能）
            } finally {
                loading.status = false;
            }
        }

        // 加载预设列表（/config/presets）
        async function loadPresets() {
            loading.presets = true;
            try {
                const result = await callApi('GET', 'config/presets');
                presets.value = Array.isArray(result) ? result : (result?.presets || []);
                // 如果后端返回空，使用兜底预设保证 UI 可用
                if (presets.value.length === 0) {
                    presets.value = FALLBACK_PRESETS;
                }
            } catch (err) {
                console.warn('加载预设失败，使用兜底预设:', err);
                presets.value = FALLBACK_PRESETS;
            } finally {
                loading.presets = false;
            }
        }

        // 应用预设（POST /config/preset/<name>）
        async function applyPreset(name) {
            closeDropdown('presets');
            if (!confirm(`确定应用预设 "${name}" 吗？当前未保存的修改将被覆盖。`)) return;

            try {
                loading.config = true;
                // v2.0.2：后端应用预设后会自行触发热重载，据实提示
                const presetResult = await callApi('POST', `config/preset/${name}`, {});
                // 重新加载配置以反映预设效果
                await loadConfig();
                const reloaded = presetResult?.reloaded !== false;
                const backendMsg = presetResult?.message;
                if (reloaded) {
                    showMessage(backendMsg || `预设 "${name}" 已应用并热生效`, 'success');
                } else {
                    showMessage(backendMsg || `预设 "${name}" 已保存，但热重载失败——请在 AstrBot 原生面板手动重载插件`, 'warning');
                }
            } catch (err) {
                showMessage(`应用预设失败: ${err.message}`, 'error');
            } finally {
                loading.config = false;
            }
        }

        // 加载日志（/config/logs?limit=N）
        async function loadLogs() {
            loading.logs = true;
            try {
                const result = await callApi('GET', 'config/logs', { limit: logLimit.value });
                const rawLogs = Array.isArray(result) ? result : (result?.logs || []);
                // 解析日志字符串数组，提取 time/level/source/message
                // 格式：[HH:MM:SS.xxx] [Core] [INFO] [main:XXXX] message
                logs.value = rawLogs.map(line => {
                    if (typeof line !== 'string') return line;
                    const match = line.match(/\[(\d{2}:\d{2}:\d{2}\.\d+)\].*?\[(\w+)\].*?\[([^\]]+)\]\s*(.*)/);
                    if (match) {
                        return { time: match[1], level: match[2], source: match[3], message: match[4] };
                    }
                    return { time: '', level: 'INFO', source: '', message: line };
                });
            } catch (err) {
                console.warn('加载日志失败:', err);
                logs.value = [];
            } finally {
                loading.logs = false;
            }
        }

        // 触发测试发言（POST /config/test_speak）
        async function executeTestSpeak() {
            if (!testSpeakForm.group_id) {
                showMessage('请先选择目标群组', 'warning');
                return;
            }
            loading.testSpeak = true;
            testSpeakResult.value = '';
            try {
                const result = await callApi('POST', 'config/test_speak', {
                    group_id: testSpeakForm.group_id,
                    topic: testSpeakForm.topic
                });
                testSpeakResult.value = typeof result === 'string' ? result : JSON.stringify(result);
                showMessage('测试发言已触发', 'success');
                // 延迟刷新状态以反映新发言
                pendingTimers.push(setTimeout(() => loadStatus(), 1000));
            } catch (err) {
                testSpeakResult.value = `错误: ${err.message}`;
                showMessage(`测试发言失败: ${err.message}`, 'error');
            } finally {
                loading.testSpeak = false;
            }
        }

        // 重置指定群状态（POST /config/reset_state）
        async function executeResetState() {
            if (!resetStateForm.group_id) {
                showMessage('请先选择目标群组', 'warning');
                return;
            }
            if (!resetStateForm.reset_types.length) {
                showMessage('请至少选择一种重置类型', 'warning');
                return;
            }
            if (!confirm(`确定重置群 ${resetStateForm.group_id} 的状态吗？操作不可撤销。`)) return;

            loading.resetState = true;
            resetStateResult.value = '';
            try {
                const result = await callApi('POST', 'config/reset_state', {
                    group_id: resetStateForm.group_id,
                    types: resetStateForm.reset_types
                });
                resetStateResult.value = typeof result === 'string' ? result : JSON.stringify(result);
                showMessage('状态已重置', 'success');
                // 延迟刷新状态
                pendingTimers.push(setTimeout(() => loadStatus(), 500));
            } catch (err) {
                resetStateResult.value = `错误: ${err.message}`;
                showMessage(`重置状态失败: ${err.message}`, 'error');
            } finally {
                loading.resetState = false;
            }
        }

        // 导出当前配置为 JSON 文件
        function exportConfig() {
            const exportData = {
                version: schemaVersion.value,
                _meta: {
                    exported_at: new Date().toISOString(),
                    schema_version: schemaVersion.value,
                    plugin: 'astrbot_plugin_smart_wakeup'
                },
                ...JSON.parse(JSON.stringify(config))
            };
            const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            const dateStr = new Date().toISOString().slice(0, 10);
            a.download = `smart_wakeup_config_${dateStr}.json`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            showMessage('配置已导出', 'success', 3000);
        }

        // 触发文件选择对话框（导入配置）
        function triggerImport() {
            // 通过 ref 引用隐藏的 input[type=file]
            const input = document.querySelector('input[type="file"][accept=".json"]');
            if (input) input.click();
        }

        // 处理导入文件
        function onImportFile(event) {
            const file = event.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = (e) => {
                try {
                    const data = JSON.parse(e.target.result);
                    if (typeof data !== 'object' || data === null) {
                        throw new Error('JSON 不是有效的对象');
                    }
                    if (!confirm('确定导入此配置吗？当前未保存的修改将被覆盖。导入后请点击"保存"以生效。')) {
                        event.target.value = '';
                        return;
                    }
                    // 合并配置到 reactive config
                    Object.keys(data).forEach(cat => {
                        if (cat === 'version' || cat === '_meta') return;
                        if (!config[cat]) config[cat] = {};
                        Object.keys(data[cat] || {}).forEach(k => {
                            config[cat][k] = data[cat][k];
                        });
                    });
                    showMessage('配置已导入，请点击"保存"以生效', 'success');
                } catch (err) {
                    showMessage(`导入失败: ${err.message}`, 'error');
                }
                event.target.value = '';
            };
            reader.readAsText(file);
        }

        // ====================================================================
        // 配置项交互方法
        // ====================================================================

        // 标记配置项已修改（Vue 响应式自动追踪，此函数仅用于显式触发 UI 更新）
        function markModified(item) {
            // Vue 3 reactive 会自动追踪深层属性变化，无需手动操作
            // 此函数存在是为了未来扩展（如打点统计、可视化反馈等）
        }

        // 检查配置项是否已修改（与上次保存的快照对比）
        function isItemModified(item) {
            const current = config[item.category]?.[item.key];
            const saved = savedConfig.value[item.category]?.[item.key];
            return JSON.stringify(current) !== JSON.stringify(saved);
        }

        // 分区折叠控制
        function toggleSection(category) {
            collapsedSections.value[category] = !collapsedSections.value[category];
            try {
                localStorage.setItem('sw_collapsed_sections', JSON.stringify(collapsedSections.value));
            } catch (e) {}
        }

        function isSectionCollapsed(category) {
            return !!collapsedSections.value[category];
        }

        // 复杂类型判断（list/template_list/object 需要全宽显示）
        function isComplexType(item) {
            // list/template_list/object 类型全宽显示
            if (['list', 'template_list', 'object'].includes(item.type)) {
                return true;
            }
            // string 类型且内容较长时全宽显示（如目标群列表、自定义话题等）
            if (item.type === 'string') {
                const hint = item.hint || '';
                const defaultVal = item.default || '';
                // v1.9.6 修正：增加"分隔"关键词检测（覆盖"用 | 分隔"场景）
                // v1.9.6 修正：多行 string（isMultilineString）也全宽显示
                if (hint.includes('逗号分隔') || hint.includes('分隔') || hint.includes('列表') || hint.includes('多个') || defaultVal.length > 50) {
                    return true;
                }
                // 多行 string（如 fetcher_rss_feeds）全宽显示
                if (isMultilineString(item)) {
                    return true;
                }
            }
            return false;
        }

        // 重置单个配置项到默认值（schema 中定义的 default）
        function resetItemToDefault(item) {
            let defaultValue;
            if (item.default !== undefined && item.default !== null) {
                defaultValue = JSON.parse(JSON.stringify(item.default));
            } else {
                // 无 default 时按类型给兜底值
                switch (item.type) {
                    case 'bool': defaultValue = false; break;
                    case 'int':
                    case 'float': defaultValue = 0; break;
                    case 'list':
                    case 'template_list': defaultValue = []; break;
                    case 'object': defaultValue = {}; break;
                    default: defaultValue = '';
                }
            }
            config[item.category][item.key] = defaultValue;
            showMessage(`已恢复 "${item.description}" 到默认值`, 'info', 3000);
        }

        // list 类型 ↔ textarea 文本互转（每行一个值）
        function listToText(arr) {
            if (!Array.isArray(arr)) return '';
            return arr.join('\n');
        }

        // list 类型输入处理（textarea → 数组）
        function onListInput(event, item) {
            const text = event.target.value;
            const arr = text.split('\n').map(s => s.trim()).filter(s => s !== '');
            if (!config[item.category]) config[item.category] = {};
            config[item.category][item.key] = arr;
        }

        // 任意 JSON 字段输入处理（template_list/object 中可能存在的复杂字段）
        function onJsonFieldInput(event, obj, key, item) {
            try {
                const val = JSON.parse(event.target.value);
                obj[key] = val;
            } catch (e) {
                // JSON 解析失败时暂不更新（避免破坏数据）
            }
        }

        // 为 template_list 添加新条目（基于 schema 中的 templates 定义）
        function addTemplateItem(item) {
            if (!config[item.category]) config[item.category] = {};
            if (!Array.isArray(config[item.category][item.key])) {
                config[item.category][item.key] = [];
            }
            // 基于 schema 模板创建新条目
            const templateSchema = item.templates?.[item.templateKey];
            const newEntry = { _uid: Date.now() + Math.random() };  // 唯一 ID 用于 v-for :key
            if (templateSchema && templateSchema.items) {
                Object.keys(templateSchema.items).forEach(fieldKey => {
                    const fieldSchema = templateSchema.items[fieldKey];
                    if (fieldSchema.default !== null && fieldSchema.default !== undefined) {
                        newEntry[fieldKey] = JSON.parse(JSON.stringify(fieldSchema.default));
                    } else {
                        // 无默认值时按类型给空值（string→空串，number→null 表示未设置）
                        newEntry[fieldKey] = fieldSchema.type === 'string' ? '' : null;
                    }
                });
            }
            config[item.category][item.key].push(newEntry);
        }

        // 删除 template_list 中的指定条目
        function removeTemplateItem(item, idx) {
            if (!confirm(`确定删除第 ${idx + 1} 条记录吗？`)) return;
            config[item.category][item.key].splice(idx, 1);
        }

        // 判断字符串配置项是否需要多行输入
        // 规则：hint 含换行、或描述长内容（如 RSS feeds、prompt 模板）时使用 textarea
        function isMultilineString(item) {
            if (!item.hint) return false;
            return item.hint.includes('\n') ||
                   item.hint.includes('每行') ||
                   item.hint.includes('多段') ||
                   item.hint.includes('多个') ||
                   item.hint.includes('格式') ||
                   item.hint.length > 100;
        }

        // 从 hint 中提取范围提示文字（如"建议 0.05~0.5"、"最小 300 秒"）
        function extractRangeHint(hint) {
            if (!hint) return '';
            // 匹配"建议 X~Y"、"范围 X-Y"、"最小/最大 X"等模式
            const patterns = [
                /建议\s*[\d.]+\s*[~\-到至]\s*[\d.]+[^\s。]*/,
                /范围\s*[\d.]+\s*[~\-到至]\s*[\d.]+[^\s。]*/,
                /最小\s*[\d.]+\s*[^\s。]*/,
                /最大\s*[\d.]+\s*[^\s。]*/,
                /0\s*[~]\s*1[^\s。]*/
            ];
            for (const p of patterns) {
                const m = hint.match(p);
                if (m) return m[0];
            }
            return '';
        }

        // ====================================================================
        // UI 交互方法
        // ====================================================================

        // 选择侧边栏分类组
        function selectGroup(groupId) {
            activeGroup.value = groupId;
            window.location.hash = groupId;  // 更新 hash 路由
            if (searchQuery.value) {
                searchQuery.value = '';       // 切换分类时清除搜索
                committedSearchQuery.value = '';
            }
        }

        // 搜索输入（带 200ms 防抖，避免高频过滤）
        function onSearchInput() {
            if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
            searchDebounceTimer = setTimeout(() => {
                searchDebounceTimer = null;
                committedSearchQuery.value = searchQuery.value;
            }, 200);
        }

        function clearSearch() {
            searchQuery.value = '';
            committedSearchQuery.value = '';
        }

        // 简单/高级模式切换
        function toggleAdvancedMode() {
            advancedMode.value = !advancedMode.value;
            showMessage(`已切换到${advancedMode.value ? '高级' : '简单'}模式`, 'info', 2000);
        }

        // 下拉菜单切换（互斥，同时只能开一个）
        function toggleDropdown(name) {
            Object.keys(dropdowns).forEach(k => {
                if (k !== name) dropdowns[k] = false;
            });
            dropdowns[name] = !dropdowns[name];
        }

        function closeDropdown(name) {
            dropdowns[name] = false;
        }

        function closeAllDropdowns() {
            Object.keys(dropdowns).forEach(k => dropdowns[k] = false);
        }

        // 面板开关
        function openStatusPanel() {
            panels.status = !panels.status;
            if (panels.status) {
                loadStatus();  // 打开时立即刷新一次
            }
        }

        function openDebugConsole() {
            panels.debug = !panels.debug;
            if (panels.debug && logs.value.length === 0) {
                loadLogs();  // 首次展开时加载日志
            }
        }

        function togglePanel(name) {
            panels[name] = !panels[name];
        }

        function closePanel(name) {
            panels[name] = false;
        }

        function openTestSpeakDialog() {
            debugTab.value = 'testspeak';
        }

        function openResetStateDialog() {
            debugTab.value = 'reset';
        }

        // ====================================================================
        // 状态自动刷新（每 5 秒）
        // ====================================================================

        function startStatusAutoRefresh() {
            stopStatusAutoRefresh();
            statusTimer = setInterval(() => {
                // 仅在状态面板打开且开启自动刷新时拉取（节省请求）
                if (autoRefreshStatus.value && panels.status) {
                    loadStatus();
                }
            }, 5000);
        }

        function stopStatusAutoRefresh() {
            if (statusTimer) {
                clearInterval(statusTimer);
                statusTimer = null;
            }
        }

        // Hash 路由解析（支持 #basic / #proactive 等直接定位分类）
        function parseHashRoute() {
            const hash = window.location.hash.slice(1);
            if (hash) {
                const group = SIDEBAR_GROUPS.find(g => g.id === hash);
                if (group) {
                    activeGroup.value = group.id;
                }
            }
        }

        // ====================================================================
        // 格式化方法（用于状态面板和日志显示）
        // ====================================================================

        // 运行时长格式化（秒 → "X天 Y时" / "X时 Y分" / "Y分"）
        function formatUptime(seconds) {
            if (!seconds || typeof seconds !== 'number') return '-';
            const days = Math.floor(seconds / 86400);
            const hours = Math.floor((seconds % 86400) / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            if (days > 0) return `${days}天 ${hours}时`;
            if (hours > 0) return `${hours}时 ${minutes}分`;
            return `${minutes}分`;
        }

        // 百分比格式化（0.85 → "85.0%"）
        function formatPercent(value) {
            if (value === null || value === undefined) return '-';
            return (Number(value) * 100).toFixed(1) + '%';
        }

        // 倍率格式化（1.5 → "1.50x"）
        function formatMultiplier(value) {
            if (value === null || value === undefined) return '-';
            return Number(value).toFixed(2) + 'x';
        }

        // 相对时间格式化（"刚刚" / "X 分钟前" / "X 小时前" / "X 天前"）
        function formatTimeAgo(timeStr) {
            if (!timeStr) return '-';
            try {
                const time = new Date(timeStr);
                const now = new Date();
                const diff = (now - time) / 1000;
                if (diff < 0) return '未来';  // 时间戳大于当前
                if (diff < 60) return '刚刚';
                if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
                if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
                return `${Math.floor(diff / 86400)}天前`;
            } catch (e) {
                return String(timeStr);
            }
        }

        // 群组 ID 简短显示（超长时截断中间部分）
        function formatGroupId(id) {
            if (!id) return '-';
            const sid = String(id);
            if (sid.length > 16) return sid.slice(0, 8) + '...' + sid.slice(-4);
            return sid;
        }

        // 日志时间格式化（HH:MM:SS）
        // v1.9.7 修复 M6：日志时间格式为 "HH:MM:SS.xxx"，new Date() 无法解析导致 Invalid Date
        // 直接用正则匹配 HH:MM:SS 前缀返回，避免 Date 解析失败
        function formatLogTime(timeStr) {
            if (!timeStr) return '';
            const str = String(timeStr);
            // 匹配 "HH:MM:SS" 或 "HH:MM:SS.xxx" 格式（日志常见格式）
            const match = str.match(/^(\d{2}:\d{2}:\d{2})/);
            if (match) return match[1];
            try {
                const time = new Date(str);
                if (isNaN(time.getTime())) return str;
                return time.toLocaleTimeString('zh-CN', { hour12: false });
            } catch (e) {
                return str;
            }
        }

        // 心流状态标签（英文 → 中文）
        function flowStateLabel(state) {
            const labels = {
                'bystander': '旁观',
                'attentive': '关注',
                'flow': '心流',
                'fatigue': '疲劳',
                'idle': '空闲'
            };
            return labels[state] || (state || '未知');
        }

        // ====================================================================
        // 生命周期
        // ====================================================================

        // 具名事件处理函数（便于在 onUnmounted 中移除监听器）
        function handleDocumentClick(e) {
            if (!e.target.closest('.dropdown') && !e.target.closest('.dropdown-menu')) {
                // 仅在有下拉打开时才操作（避免无谓的状态变更）
                if (anyDropdownOpen.value) {
                    closeAllDropdowns();
                }
            }
        }

        function handleDocumentKeydown(e) {
            if (e.key === 'Escape') {
                if (anyDropdownOpen.value) {
                    closeAllDropdowns();
                } else if (panels.status) {
                    panels.status = false;
                } else if (panels.debug) {
                    panels.debug = false;
                }
            }
        }

        // v1.9.7 新增 M7：未保存修改时提示用户（beforeunload）
        function handleBeforeUnload(e) {
            if (hasUnsavedChanges.value) {
                e.preventDefault();
                e.returnValue = '';
                return '';
            }
        }

        onMounted(() => {
            // 启动初始化
            init();

            // 监听 hash 变化（支持浏览器前进后退）
            window.addEventListener('hashchange', parseHashRoute);

            // 全局点击关闭下拉菜单（点击 dropdown 外部时关闭）
            document.addEventListener('click', handleDocumentClick);

            // 监听 ESC 关闭面板/下拉
            document.addEventListener('keydown', handleDocumentKeydown);

            // v1.9.7 新增 M7：页面关闭/刷新时提示未保存修改
            window.addEventListener('beforeunload', handleBeforeUnload);
        });

        onUnmounted(() => {
            // 清理定时器（M4）
            if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
            if (statusTimer) clearInterval(statusTimer);
            pendingTimers.forEach(t => clearTimeout(t));
            pendingTimers.length = 0;

            // 移除事件监听器（M3）
            window.removeEventListener('hashchange', parseHashRoute);
            document.removeEventListener('click', handleDocumentClick);
            document.removeEventListener('keydown', handleDocumentKeydown);
            // v1.9.7 新增 M7：移除 beforeunload 监听
            window.removeEventListener('beforeunload', handleBeforeUnload);
        });

        // ====================================================================
        // 暴露给模板的所有响应式数据和方法
        // ====================================================================
        return {
            // 状态
            loading,
            loadingText,
            message,
            schema,
            config,
            status,
            presets,
            logs,
            searchQuery,
            committedSearchQuery,
            activeGroup,
            advancedMode,
            panels,
            dropdowns,
            autoRefreshStatus,
            collapsedSections,
            debugTab,
            logFilter,
            logLimit,
            testSpeakForm,
            resetStateForm,
            testSpeakResult,
            resetStateResult,

            // Computed
            schemaVersion,
            totalItems,
            totalCategories,
            sidebarGroups,
            currentGroupIcon,
            currentGroupLabel,
            currentGroupHint,
            currentGroupItemCount,
            currentGroupAdvancedCount,
            currentGroupModifiedCount,
            filteredItems,
            groupedFilteredItems,
            hasUnsavedChanges,
            groupIds,
            filteredLogs,
            anyDropdownOpen,

            // 消息方法
            clearMessage,

            // UI 交互方法
            onSearchInput,
            clearSearch,
            toggleAdvancedMode,
            selectGroup,
            toggleDropdown,
            closeDropdown,
            closeAllDropdowns,
            openStatusPanel,
            openDebugConsole,
            togglePanel,
            closePanel,
            openTestSpeakDialog,
            openResetStateDialog,
            toggleSection,
            isSectionCollapsed,
            isComplexType,

            // 业务方法
            saveConfig,
            resetConfig,
            loadStatus,
            applyPreset,
            loadLogs,
            executeTestSpeak,
            executeResetState,
            exportConfig,
            triggerImport,
            onImportFile,

            // 配置项交互
            markModified,
            isItemModified,
            resetItemToDefault,
            listToText,
            onListInput,
            onJsonFieldInput,
            addTemplateItem,
            removeTemplateItem,
            isMultilineString,
            extractRangeHint,

            // 格式化方法
            formatUptime,
            formatPercent,
            formatMultiplier,
            formatTimeAgo,
            formatGroupId,
            formatLogTime,
            flowStateLabel
        };
    }
}).mount('#app');
