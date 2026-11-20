# kitty 动画与 Agents 侧边栏性能优化交接

更新日期：2026-11-20。本文记录尚待实施的优化项、代码证据、验证方法和风险；不是性能收益承诺。

## 1. 当前状态与边界

上一轮已完成的源码改造：

- pane 弹簧动画从 `glReadPixels → CPU 像素 → CGImage → CALayer` 改为 GPU 内纹理复制与 OpenGL 四边形合成。
- 侧边栏 working spinner 从 30 Hz timer 驱动整行重绘，改为独立 `CAShapeLayer` 和 Core Animation 旋转。
- spinner 在 idle、隐藏、移除时停止；Reduce Motion 下保留静态圆弧。
- pane 动画保留时长、回弹配置和实时终端内容，连续布局变化从当前显示位置衔接。

这轮性能检查时，上述 GPU 改造尚未提交、未部署到 `/Applications/kitty.app`。**接手时重新确认源码、提交和安装包状态，不能把运行中的安装版默认当成当前工作区构建。**

已有验证记录：

- `./dev.sh build --full` 成功。
- `./test.py --module agents`：63 个测试通过。
- 原生 smoke 覆盖不透明和 `background_opacity=0.75`、快速布局切换、动画中关闭 pane、spinner 状态变化、隐藏重显及退出。
- 在测试进程内覆盖 Reduce Motion 查询，验证静态 spinner；没有修改系统设置。
- 运行日志：`GL version string: '4.1 Metal - 91.7' Detected version: 4.1`。
- 屏幕录制权限限制了系统窗口截图；使用应用内部 framebuffer 和原生视图快照检查画面。
- 本地证据在 `build/gpu-verification/results-bxftl3_2/` 等目录，属于可清理构建产物，不保证随 Git 交付。临时验证程序已删除，需要时重建。

本文中的行号是检查时定位提示，后续以符号名为准。

## 2. 优先级与实施顺序

| 编号 | 优先级 | 优化项 | 主要作用 | 建议顺序 |
|---|---|---|---|---|
| P1 | 高 | 侧边栏内容刷新与 pane 几何刷新解耦 | 避免状态、标题变化触发终端 relayout | 1 |
| P2 | 高 | 同一观察批次共享进程详情 | 减少 cmdline/cwd 等重复系统调用 | 2 |
| P3 | 高 | 原生侧边栏按行差量更新 | 减少字符串、数组构造和无变化行重绘 | 3 |
| P4 | 中高 | pane 内容纹理复用，减少整屏复制 | 降低动画期间 GPU 带宽和重复渲染开销 | 4 |
| P5 | 中 | 按需提取 agent 检测屏幕快照 | 减少屏幕序列化和重复规则检测 | 5 |
| P6 | 中 | 有界复用动画资源及预计算弹簧系数 | 减少重复分配与小粒度计算 | 6 |
| D1 | 单独调查 | display link 是否频繁重建 | 排查线程 churn、唤醒和窗口系统调用 | 可独立取证 |

前 3 项改动较集中，优先于重写渲染后端。不要把全部优化合并成一次无法归因的大改动。

## 3. P1：内容刷新不要触发 pane relayout

### 代码证据

- `kitty/boss.py`：`Boss._refresh_agent_sidebar_scope()`，检查时约 2035–2072 行。
- `kitty/tabs.py`：`TabManager.set_agent_sidebar_rows()`，约 1921–1924 行。
- 当前 rows 改变后会调用 `self.resize()`；`TabManager.resize()` 更新原生侧边栏并逐个 `tab.relayout()`。
- **已有去重**：Boss 比较 `tuple(rows) != tm.agent_sidebar_rows`，完全相同的 rows 不会进入这条更新链。不要错误描述为每秒无条件 relayout。

### 改造方向

- 标题、详情、状态、active 变化仅更新 sidebar 内容。
- 只有显隐、宽度、字体/缩放或实际可用内容区域变化才触发布局。
- 区分内容脏与几何脏，复用现有刷新约定；不要引入第二套布局状态源。

### 验收

- 连续标题变化、working/idle/done、焦点变化时，侧边栏正确更新，但 pane relayout 和 PTY resize 次数不随内容刷新增长。
- 切换显隐、拖动宽度、改变字体/显示缩放后，终端几何和 PTY 行列数正确。
- native tabs 共享侧边栏、session restore、普通 tab bar 同时存在时仍正确。

### 风险

侧边栏显隐或宽度变化漏掉真正必要的几何刷新；状态刷新改变动画或焦点行为。

## 4. P2：观察批次共享进程详情

### 代码证据

- `kitty/agents.py`：`AgentTracker.refresh_window()`、`agent_identity()`、`snapshot_for_window()`。
- `kitty/child.py`：`Child.foreground_processes`、`Child.process_desc()`，约 709–724 行。
- `foreground_processes` 每次查询前台进程组，并逐 PID 获取 cmdline、cwd。
- `CachedProcessData` 当前缓存进程组映射，不等于缓存 PID 详情。
- `kitty/boss.py`：`refresh_agent_sidebars()` 每秒将全部窗口标脏；`flush_agent_sidebar_updates()` 逐个观察。
- `kitty/agent_onboarding.py`：`AgentIntegrationPrompts.poll()` 也会获取进程信息；其 `cached_process_data()` 使用 refresh=True，可能额外刷新进程组映射。
- `kitty/rc/agent_wait.py`：有 pending waiter 时，以 0.05 秒周期调用 `observe_pending()`。
- `kitty/agent_broker.py`：0.05 秒 drain 检查绑定身份。是否与其他扫描重复，应测调用量后确认。

### 改造方向

- 在一次观察批次内获取一致的前台进程描述，供 identity、agent 检测、cwd 获取共用。
- 对多个窗口共享批次级进程组映射；仅在确有需要时加载进程详情。
- 必须处理前台 pgrp 变化、进程退出、PID 重用、会话身份变化；避免长期缓存返回旧身份。
- 不直接删除周期兜底：未接入 hook 的程序仍需要被发现，waiter 的响应时延契约也必须保留。

### 验收

- 为一轮观察统计 `tcgetpgrp`、进程组映射、cmdline、cwd 系统查询次数，确认重复调用减少。
- 覆盖 shell → agent → shell、agent 重启、PID 重用、SSH、多个前台进程、Node process.title 改写 argv。
- 核对 generation、done 通知、broker capability 撤销、agent wait 的可见行为不变。
- 测量随 pane 数量增长的主线程耗时，不能只跑单窗口。

## 5. P3：原生侧边栏按行差量更新

### 代码证据

- `kitty/glfw.c`：`set_agents_sidebar()`，约 3290–3404 行，每次分配多组临时数组并解包所有 rows。
- `kitty/cocoa_window.m`：`-[KittyAgentsSidebarView setRows:...]`，约 1654–1748 行。
- 每次构造 8 组数组、NSString、分组 children 和 row indices。
- `same_structure` 仅避免部分结构 reload；结构未变时仍遍历所有行，设置 row/cell 需要重绘并更新 tooltip/spinner。
- `drawNode:inRect:` 每次绘制也创建段落样式、属性字典和 attributed string。

### 改造方向

- 使用稳定 window ID / group key 比较内容与结构变化。
- 结构变化才更新 outline；内容变化仅更新受影响行。
- 不变文字、图标、tooltip、颜色无需重新设置；不因标题或周期刷新重启 spinner 的 rotation。
- 如测得文字排版为热点，再缓存每行文字布局，按文本、宽度、字体、缩放、外观失效；不要无界缓存。
- Python 层已有相同 rows 的去重；重点覆盖 rows 真有变化以及 resize 等其他调用原生 bridge 的路径。

### 验收

- 单个 agent 改标题/状态时，不变行的重绘和 native 对象分配明显减少。
- 分组新增删除、排序、折叠、标题相同的多个 tab、选中行消失时保持身份正确。
- 深浅外观切换、Retina 缩放、宽度拖拽、键盘导航、滚动复用、菜单追踪正常。
- 记录更新前后 draw 次数、分配次数、主线程耗时，确认 spinner 不因刷新归零。

## 6. P4：动画内容纹理复用与最终合成优化

### 代码证据

- `kitty/shaders.c`：`draw_pane_animation()`，约 3783–3841 行。
- 每个动画帧执行全 viewport 的 `glCopyTexSubImage2D()`，再清除 central region、采样各 pane 绘制。
- 已消除 GPU → CPU 回读，但**尚未消除 GPU 内整屏复制和动画期间完整终端重绘**。
- `kitty/child-monitor.c`：`prepare_to_render_os_window()` 中 active pane animation 持续要求重绘。
- 现有最终合成流程：`start_os_window_rendering()`、`stop_os_window_rendering()`、`run_custom_end_shader()`；已有中间 framebuffer/纹理可研究复用。

### 理论负载，不是实测带宽

| 实际 framebuffer 大小 | 一张 RGBA8 | 60 fps 复制负载 | 160 fps 复制负载 |
|---|---|---|---|
| 1800 × 1200 | 8.24 MiB | 0.52 GB/s | 1.38 GB/s |
| 5120 × 2880 | 56.25 MiB | 3.54 GB/s | 9.44 GB/s |

仅为 width × height × 4 × fps，不包含读写两侧、额外 pass、驱动优化和合成，不代表实测吞吐。显示器分辨率也不等于每个窗口的 framebuffer 分辨率。

### 改造方向

1. 区分“终端内容/光标/边框/shader 变化”和“仅动画几何位置变化”。
2. 仅几何变化时复用内容纹理，仍按显示帧合成，不重做终端渲染和复制。
3. 如实测证明复制仍是热点，再让最终内容直接进入可采样离屏纹理，减少 copy pass。
4. 静态 pane 与持续输出 pane 可以考虑不同失效粒度，但先选简单可验证方案。

### 验收

- 统计无输出动画和持续输出动画的内容 render、纹理 copy、合成帧次数。
- 使用 GPU 时间查询或 Instruments 测量 pass 耗时，异步收集结果，避免测量本身强制同步。
- 比较不透明/半透明、背景图、圆角阴影、cursor trail、自定义 shader、多个 OS 窗口。
- 动画中光标闪烁、输出、图片动画不得冻结；快速 retarget、tab 切换、关闭、live resize、Reduce Motion 不残影。
- 验证 sRGB、premultiplied alpha、裁剪和 Retina 坐标；防止纹理同时作为采样源与渲染目标。

### 风险

现有中间纹理含共享资源，必须检查跨窗口生命周期；缓存失效漏项会把实时内容变成过期截图。不要以降低刷新正确性换取较低计数。

## 7. P5：按需提取 agent 屏幕快照

### 代码证据

- `AgentTracker.refresh_window()` 当前先执行 `window.agent_detection_snapshot()`，之后 `snapshot_for_window()` 才判断是否识别到 agent。
- `Window.agent_detection_snapshot()` 使用 `screen.as_text_non_visual()`，拼接并 splitlines 当前屏幕，约 `kitty/window.py:2388–2401`。
- 该提取读取当前 live rows，不是历史 scrollback；不要误判为遍历整个历史缓冲。

### 改造方向

- 先判别 agent，非 agent 不序列化屏幕。
- 无相关内容、标题或 progress 变化时复用检测快照。
- 按 manifest 的实际 authority / fallback 规则决定是否需要屏幕证据；不是所有有 hook 的 agent 都能完全跳过屏幕。
- 如仅需要某些 region，评估局部提取，但必须保留 blank-line 计数、Unicode、软换行和大小写语义。

### 验收

- 非 agent、静态 agent、持续输出 agent 分别记录提取次数与耗时。
- 覆盖用户滚动历史时仍检测 live bottom、alternate screen、prompt 多阶段重绘、working → idle 防抖、unseen done。
- 保持 title/progress 变化触发失效；不能因为 screen 文本未变而错过状态变化。

## 8. P6：有界复用资源与弹簧计算

### 代码证据

- `draw_pane_animation()` 非 active 时删除 texture，下次动画重新 `glGenTextures/glTexImage2D`。
- `kitty/pane-animation.h`：`pane_animation_progress()` 每次调用计算阻尼相关函数；每 pane 位置和尺寸分别采样。
- `cocoa_prepare_pane_animation()` retarget 时按 pane ID 扫描旧 frames；大量 pane 时可能为二次复杂度。

### 改造方向

- 根据真实窗口大小和显存预算决定纹理缓存策略，明确 resize/关闭/长时间闲置释放时机。
- 预计算仅随 retarget 改变的阻尼系数；同一帧相同 spring 参数可复用计算。
- pane 匹配只有在数量和 profile 证明值得时才引入索引，不为少量 pane 加复杂容器。

### 验收

连续分屏/关闭时分配次数降低；显示器切换、窗口 resize、关闭后无资源残留；内存峰值受控。保持端点、回弹上限、反向衔接、零回弹单调性。

## 9. D1：display link 重建频率调查

### 已有证据与限制

- 对 PID 11117 的安装版 kitty 执行了 3 秒 `sample`，证据文件：`build/kitty-performance-sample.txt`。
- 采样路径为 `/Applications/kitty.app/Contents/MacOS/kitty`，不是当前 GPU 改造的开发构建。
- 该 sample 中有 88 个不同的 `CVDisplayLink` 线程条目，包含等待及采样期间新生/退出的线程；**不能据此断言泄漏或 88 个同时活跃线程**。
- 主线程栈包含 AppKit/CoreText 文字排版和绘制；旧 spinner 重绘可能贡献其中，但尚未做因果对照。
- sample 报告 physical footprint 2.4G、峰值 2.9G，不等同于 RSS，也不能单凭该数字断言泄漏。
- `ps` 两次瞬时读数为 66.2% 和 72.7% CPU；没有控制当时用户负载，不能称为 idle 基线。

### 代码入口

`glfw/cocoa_displaylink.m`：`_glfwRequestRenderFrame()`、`_glfw_create_cv_display_link()`、`_glfwShutdownCVDisplayLink()`、`_glfwClearDisplayLinks()`。

当前实现对超过一秒未服务的请求存在 display link 重建逻辑。`kitty/glfw.c` 的 `request_frame_render()` 接入该路径。

### 下一步取证

- 在独立开发实例中统计 create/start/stop/recreate、未服务时间、pending dispatch 和当前显示器 ID。
- 区分正常显示器迁移、休眠/屏保恢复、主线程阻塞和错误超时重建。
- 对照前台/后台、静态/持续输出、60/高刷新率、多显示器、休眠恢复。
- 只有确认高频无效重建后再修改超时与调度；保留真正 stuck display link 的恢复能力，不通过简单延长超时掩盖问题。
- 临时 instrumentation 必须清理；未测量前不迁移整个后端或直接删除 watchdog。

## 10. 接手后的验证与工程约束

1. 明确被测二进制、编译所用 Python、是否包含 GPU 改造，再建立对照数据。
2. 不修改用户正在运行的实例做性能实验；使用独立配置、禁用自动 session 持久化的测试实例。
3. **不要从 `/tmp` 执行二进制文件或加载测试动态库**：本机安全软件会拦截。测试源码、可执行程序、动态库和 Go 临时执行产物放仓库 `build/` 下，必要时将 `TMPDIR/GOTMPDIR` 指向该目录。仓库 CI 准备脚本自身使用 `/tmp/slang`，接手前也应留意这一现有行为，不要另行复制这种约定。
4. 必须遵守仓库构建要求：准备命令为 `python3 .github/workflows/ci.py build`，需选择支持项目语法的 Python；本机系统 `/usr/bin/python3` 是旧版，前次使用 `/opt/homebrew/bin/python3.14`。
5. 构建使用 `./dev.sh build`；不要绕过项目直接 `go build` / `go test`。
6. 曾遇增量构建旧 launcher 链接 Homebrew Python，而新扩展链接 dependencies Python，导致进程加载两份 runtime、在 `PyModule_Create2` 崩溃。全量 `./dev.sh build --full` 后恢复；这是构建环境问题，不是已证实的 GPU 动画崩溃。再次出现时用 crash report 和 `otool -L` 核对，不盲目改业务逻辑。
7. Python 测试按方法名选，或使用 `./test.py --module agents`；`./test.py agents` 不是模块选择语法。
8. 原生 watcher 运行时可能开启 Python 优化，**不要使用 `assert` 包裹有副作用的验证调用**。用显式条件和异常，防止断言被优化删除、出现假通过。
9. 测量最少记录：主线程 CPU、每次刷新耗时、系统查询次数、relayout/PTY resize 次数、row draw 次数、内容 render/copy/composite 次数、GPU 帧耗时、内存峰值。
10. 每项优化独立验证；只有确认代码、输入和环境未变时才复用上一轮证据。没有实测时只记录待验证假设，不写收益百分比。

## 11. 完成清单

- [x] P1：实际独立实例中内容更新无 pane relayout/geometry 调用；显隐、宽度和字体变化的终端行列验证通过。
- [x] P2：批次进程详情共享，嵌套/退出/前台切换/同 PID 新描述回归与多 pane 调用量对照通过。
- [x] P3：原生行差量更新；实际原生探针和浅/深外观截图通过，保持选择、分组、菜单和 spinner 连续性。
- [x] P4：静态内容动画 render/copy 减少；实时输出、光标、palette-only、透明度、背景图/shader/动态图像/多窗口和 Reduce Motion 实际场景通过。
- [x] P5：按需提取检测快照，复用已有文本 generation；manifest fallback 与 raw service / presentation 分离回归通过。
- [x] P6：每窗口资源缓存有界；实际 idle 释放与 resize 释放已观察，弹簧 80,024 样本数值对照通过。
- [x] D1：获得 display link 重建频率与原因；已根据真实探针修正请求状态转换，保留 watchdog。
- [x] 已记录对照数据、配置、硬件与最终二进制身份；临时程序/动态库/计数日志已清理，用户可见 changelog 已更新。

## 12. 本轮实施与实测（2026-11-20）

### 环境与证据边界

- 本轮基线为 `73d2f445d` 加接手时已暂存的 GPU pane 动画与 Core Animation spinner 改造，不是安装版或裸 HEAD。
- 独立探针硬件：Apple M4（10 核 CPU / 10 核 GPU）、16 GiB；两块 5120×2880 显示器，逻辑尺寸均为 2560×1440，刷新率分别 160 Hz 和 60 Hz。
- 准备命令首先因 Homebrew Python 的 PEP 668 限制失败；随后使用仓库 `build/performance-venv` 的 Python 3.14，并通过内存中覆盖 CI 模块的 Slang 路径到 `build/performance-slang` 完成相同准备流程。未修改系统 Python 或 CI 源码。
- 没有部署或修改 `/Applications/kitty.app`；探针和编译产物均在仓库 `build/` 下。下述计数是指定输入的实测，不是生产负载或整机 CPU 收益承诺。
- GPU 时间测量受环境限制：`xctrace list templates` 报错要求完整 Xcode，当前仅安装 Command Line Tools；现有 GL wrapper 未暴露 `GL_TIME_ELAPSED` / 64 位 timer-query 结果接口。本轮不为临时测量扩展生产 GL 加载器；render/copy/composite 计数不等同于 GPU 耗时或实测带宽。

### D1：已定位并修正请求状态转换

`_glfwRequestRenderFrame()` 不再清除已排队的 `pending_dispatch`；watchdog 仅在没有排队主线程 dispatch 时判断一秒未服务。`_glfwDispatchRenderFrame()` 在调用窗口回调前消费旧请求，保留回调中产生的下一帧请求。未延长超时，也未移除无回调时的恢复机制。

真实 Cocoa / CoreVideo 独立窗口探针直接调用当前 `glfw/cocoa_displaylink.m`；除计数包装外使用真实 CVDisplayLink：

| 场景 | 修改前 | 修改后 |
|---|---|---|
| 连续回调内请求，3 秒 | 新增 1 次窗口回调后停止 | 新增 480 次窗口回调；link 未重建 |
| 主线程阻塞 1.3 秒后再次请求 | 即使 dispatch 已排队仍重建 1 次 | 保留排队 dispatch，重建 0 次 |
| 10 次间歇请求 | 1 个 link；每次请求 start/stop | 同样 1 个 link；未为降低计数改变空闲停止策略 |
| 后台窗口连续请求 1 秒 | 未建立对照 | 158 次回调，无重建 |
| 移至第二块 60 Hz 显示器，1 秒 | 未建立对照 | 60 次回调，为新显示器正常新建 1 个 link |

确定性回归通过 `./test.py macos_displaylink_scheduling`：覆盖排队期间重复请求不重复 dispatch、回调内请求下一帧、真正 running 但无回调的 link 触发 watchdog、空闲 stop 后复用同一个 link。真实探针中尝试用 NULL CoreVideo callback 模拟 stuck 会使 link 停止，因此该注入不构成恢复证据；恢复路径由确定性适配器验证。未操作系统休眠、屏保或更改显示器设置。

证据：`build/performance-displaylink-baseline.jsonl`、`build/performance-displaylink-multidisplay.jsonl`；构建产物不保证随 Git 交付。D1 独立代码审阅通过，无需迁移 display-link 后端。

### P4 / P6：内容失效复用与有界资源

- 仅动画几何变化时跳过终端、边框和最终 shader 内容渲染及纹理 copy；先从缓存恢复完整 backbuffer，再合成 pane，避免假设 swap 后 backbuffer 保留。
- 内容路径仍执行已有 dirtiness 检查；额外覆盖适用的 live / synchronized-output frozen color profile 的 dirty 标记，防止仅 OSC 21 改色时缓存过期。
- 每 OS window 最多保留一张动画纹理；非动画期间仅缓存不超过 64 MiB 的纹理，最后使用两秒后释放，resize 与窗口销毁也释放。不为少量 pane 引入 ID 索引或新渲染后端。
- 弹簧阻尼相关 frequency / sine coefficient 在 retarget 时预计算，位置与尺寸阻尼相同则同帧复用 progress。对旧公式 80,024 个样本的差分验证最大误差为 0；端点、零回弹单调性、百分比/像素回弹上限与反向 retarget 连续性通过。
- 真实日志还观察到纹理 `release=1 resize=0 idle=1`（半透明实例 13.499 / 17.815 秒），以及不透明实例 resize 释放后按新尺寸分配；空闲释放不依赖下一次动画开始。

首次不透明独立实例的计数窗口（截图请求位于窗口之外）：

| 场景 | 内容 render | 仅几何帧 | texture copy | composite | 窗口内 texture allocation |
|---|---|---|---|---|---|
| 静态内容 | 2 | 96 | 1 | 97 | 0 |
| 持续输出 | 40 | 29 | 17 | 46 | 0 |
| 仅光标闪烁 | 8 | 88 | 7 | 95 | 0 |

这些是修改后实际计数，不是修改前后同帧率基准。旧版 active 动画每 composite 都 copy；本轮尚不据此声称 CPU/GPU 收益百分比。静态窗口 5120×2598 framebuffer；resize 后 2100×1400。主线程 CPU / wall 分别为静态 79.55 ms / 1.678 s、输出 62.43 ms / 1.208 s、光标 60.87 ms / 1.015 s；整个实例观测到峰值 RSS 287,244,288 bytes，包含截图分配，不代表纯动画显存。

已查看内部 framebuffer 的 retarget、实时输出和 resize 截图，文字方向、红字蓝底、live 递增内容正确。`background_opacity=0.75` 图像背景 alpha 为 191（不透明为 255），未出现重复叠加变暗。半透明首次场景的光标窗口被其他应用遮挡，后续仅 RC focus 也仍是 invisible；这些零帧计数不计作优化收益或有效光标验证。

证据：`build/pane-evidence-opaque/`、`build/pane-evidence-alpha/`；完整后续验收以实际可见窗口场景为准。

原生可见性探针确认后续实例实际位于非活动 Space，`NSWindow.occlusionState=8192` 未包含 SDK 定义的 Visible 位（`1<<1`）。生产可见性判断正确；测试只调整本进程窗口的 Space/floating 属性，不修改其他应用、不关闭 occlusion 判断。

通过 `ioreg` 的 `IOConsoleUsers` 确认中途 `CGSSessionScreenIsLocked=True`，当时停止了可见性实验；用户解锁后完成下列原生验收。未解锁系统或绕过生产遮挡判断。开发 bundle 的 LaunchServices 启动另因未密封的开发签名失败，最终使用独立 flat 开发实例，未签名或部署安装版。

用户解锁后完成有效窗口观察：静态 2 content render / 93 geometry-only / 1 copy / 94 composite，持续输出 21 / 60 / 19 / 79，palette-only 7 / 89 / 5 / 94；截图请求均在计数窗口之外。另一次半透明光标场景为 8 content/copy / 95 composite。palette-only 使用隐藏光标、固定 SGR31 文字，仅发送 OSC 21 的 palette index 1 更新；多次 copy 发生在截图前，证实改色触发失效。最终截图呈现洋红文字，但截图本身不作为中间帧颜色变化的证明。

验证脚本已修正三类前置条件：实际切换到不同 layout 才产生几何动画；Cocoa 激活后等待可见性状态稳定；`cursor_stop_blinking_after=0` 避免超过默认 15 秒 OS-window 键盘空闲阈值后误判光标冻结。均未修改生产行为。

组合场景正常退出：`background_opacity=0.75`、tiled 背景图、`northern-lights` 持续 shader、cursor trail、animated GIF、两个 OS window。持续 shader 场景各计数窗口 geometry-only 均为 0（必须持续刷新，而不是缓存冻结）；静态/输出/光标/palette/图像的 copy=composite 分别为 15/21/13/12/13。共 6 次纹理分配、6 次释放。已查看内部 framebuffer：背景图、绿色 GIF 帧、文字与 cursor trail 均可见，无明显裁剪/方向错误。

组合场景日志有一条 AppKit IMK mach-port warning，以及 teardown 时 child 已退出的 resize 提示，未抑制；无 GL 错误或进程崩溃。证据：`build/pane-evidence-extended/`。

Reduce Motion 单独场景正常退出：在测试进程内覆盖 NSWorkspace 查询，未改系统偏好。静态、输出、光标分别 content render 1/28/7；三个计数窗口均为 0 geometry-only / copy / composite / texture allocation。实时输出继续渲染，pane motion 被关闭。证据：`build/pane-evidence-reduce-motion/{summary,reduce-motion}.json`。

### P3：稳定身份与按字段差量更新

原生模型按 window ID 保留 entry；group key 仍为 outline 身份。只有结构变化重建 outline；未变字段不创建新 NSString，未变行不设置 tooltip / redraw / spinner。bridge 在不超过 32 行与 32 个折叠组时使用栈内临时数组，更大时一次有界大小检查后的分配。未增加文字布局缓存。

24 行实际原生探针通过：连续 100 次相同输入没有 UI 更新调用；改单行标题只替换该 label 并 dirty 一个 cell，不重启 rotation；改单行状态更新一个 spinner / cell；active 转移更新两个 cell / row。分组同名不同 key、折叠/展开、排序、选中项删除、菜单 window ID 保持正确。记录对象地址时保留原对象，避免地址复用假阳性；这是对象复用证据，不是 malloc 总数。

同一探针、同样 1000 次更新，原暂存版本与修改后版本分别在独立进程测得主线程 CPU 时间：

| 行数 | 相同内容：原版 → 修改后 | 单行标题：原版 → 修改后 |
|---|---|---|
| 1 | 11.526 → 1.733 ms | 11.448 → 3.633 ms |
| 8 | 44.552 → 3.791 ms | 44.225 → 5.937 ms |
| 32 | 89.228 → 9.548 ms | 89.367 → 11.726 ms |

探针不改变系统主题。父任务已查看显式 Aqua / DarkAqua 不透明原生截图：标题、分组、状态、选中背景和 Unicode 文字正常。基线从接手时 Git index 的原始 sidebar 类提取，仅重命名 Objective-C 类以避免碰撞，真实 setRows/draw/spinner 代码不变；基线只运行更新基准，不运行需要宿主隐藏 C 符号的菜单/resize 路径。上述耗时不包含全程序帧呈现，也不代表整机 CPU 收益。

P3 独立审阅覆盖 MRC 生命周期、移除项、selection/collapse/scroll/menu、外观/宽度/DPI/icon/grouping 失效和 bridge 对齐/异常清理，无实质问题。证据：`build/sidebar-staged-results.json`、`build/sidebar-delta-results.json`、`build/sidebar-delta-{light,dark}.png`。

### P1 / P2 / P5：布局、批次身份与屏幕证据

- 行内容更新只走原生 sidebar 内容接口；显隐、初次 manager 建立和宽度/字体变化仍显式布局，不新增几何状态源。
- 进程缓存仅存活于一轮观察上下文：进程组映射、每 Child 前台 pgrp/PID 列表和每 PID cmdline/cwd 描述供 identity、检测和 cwd 复用。嵌套上下文不刷新外层快照，最外层退出清空；下一轮重新查询。Boss flush、waiter、service poll、broker drain 共享各自一轮批次，保留原周期兜底与 waiter 时延。
- `AgentTracker.live_snapshot()` 共享原始屏幕证据，而非 debounce/unseen-done 后的显示状态；service、状态查询和 prompt 门禁复用它。非 agent 不提取屏幕，稳定 agent 的屏幕证据按现有 `Screen.search_generation`、实际 Screen 对象、agent/session、尺寸、title/progress 失效。未新建 native 版本号或长期 PID 缓存。explicit read/explain 仍按请求输出屏幕。
- full-lifecycle 仅有可归属的语义报告才跳过屏幕；没有 report、错误 session 或 OpenCode 尚无 root session 时保留 fallback。

受控系统调用适配器驱动真实原暂存版本和修改后 Child / AgentTracker，三轮静态观察：

| pane 数 | 观察数 | tcgetpgrp：前 → 后 | cmdline：前 → 后 | cwd：前 → 后 | 屏幕提取：前 → 后 |
|---|---|---|---|---|---|
| 1 | 3 | 9 → 3 | 6 → 3 | 9 → 3 | 3 → 1 |
| 8 | 24 | 72 → 24 | 48 → 24 | 72 → 24 | 24 → 8 |
| 32 | 96 | 288 → 96 | 192 → 96 | 288 → 96 | 96 → 32 |

各输入进程组映射均为三次（每轮一次），原版已有共享映射，本轮收益来自详情与前台查询去重。此适配器不是真实 syscall 延迟基准；8/32 pane 的 Python CPU 分别从 0.523/1.829 ms 变为 0.610/2.236 ms（总三轮），说明批次管理有小额 Python 开销，不能用该脚本声称 CPU 百分比改善。实机系统查询耗时收益仍取决于进程规模。

已执行回归：原始状态与 held done 分离、native Screen 文本变化/对象替换、title/progress 变化、非 agent 零提取、无可用 full-lifecycle report 保留 fallback；nested batch 内 cwd 一致、退出重查、前台 pgrp 切换和同 PID 新 cmdline/cwd。`agents` 66 项、control 10 项、service native-free 16 项通过；额外 P1/P2 两项回归通过。原 child 19 项（3 项 Linux-only 跳过）、wait 13 项、onboarding 10 项、最终 broker 9 项通过。

P1/P2/P5 独立窄审阅通过；broker 新加一轮批次后单独审阅通过。证据：`build/performance-observation-results.json` 与各 `build/performance-*-tests.log`。测试模块必须逐个 `--module` 调用，重复 `--module` 只选择最后一个。

P1 实际独立原生实例（锁屏不影响显式 Python 布局调用）得到终端列×行：初始 128×48，打开侧栏 98×48；100 次标题/状态/active 内容更新后仍 98×48，`Tab.relayout` 与 `Window.set_geometry` 调用均为 0。侧栏增至 40 列后 88×48，字体增大后 77×40，隐藏侧栏后 112×40。打开侧栏触发布局/geometry 各一次。场景结果已写入 `build/sidebar-geometry-runtime/result.json`；程序退出请求未及时关闭导致外层 60 秒 timeout，不能称整个进程正常退出，但布局断言与数值结果在超时前完成。

补充 P5 live-bottom 回归通过：native Screen 有历史且用户滚动后，仍复用当前 live bottom；alternate screen 进入与退出正确失效并恢复 working / idle 检测。

### 最终构建与清理

- `./dev.sh build --full` 成功；palette-only 失效修正后增量构建成功；移除五条临时 render/copy/composite/allocation/release 计数日志及 visibility 诊断后，最终 `./dev.sh build` 成功。
- 最终 launcher 与 extension 均链接仓库 dependencies 的 Python 3.14，不混用 Homebrew runtime。
- 最终 SHA-256：launcher `c6ab298a4d0933423b426770c5a7c28f2155e565a2c0300a8e090144243d3cfd`；`fast_data_types.so` `07b1c2b7d1c4b6fc8cc4c1a1e11f0ac0e3adc53833db33cce567ef83e1e9f125`；`glfw-cocoa.so` `875cbc64fdb7b9538a9011265b9ea2b3efa8e6505c89fbcbdc8333417199df8a`。
- 未提交、推送或部署；保留接手时已有暂存改动。临时测量结果和截图位于 `build/`，不作为永久诊断 API。GPU pass 纳秒耗时、整机生产 CPU 收益及真实休眠恢复没有实测，未作收益承诺。
- 清理计数日志后的最终原生 smoke 通过：独立半透明实例完成分屏、快速 retarget、实时内容、framebuffer 截图及正常退出；最终 `macos_displaylink_scheduling` 回归通过（1 项）。已查看清理后 framebuffer，两个 pane 内容均可见。
- 本轮临时 Python/C/Objective-C 验证源码、基线 dylib 与临时可执行程序已删除，结果 JSON/log/PNG 保留；永久回归保留在 `kitty_tests/`。未执行无关全仓库测试。
