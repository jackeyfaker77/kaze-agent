# Kaze 小风鼬桌宠

基于用户选定的坐姿小风鼬，延续奶白色身体、绿色领巾、蓝色卷尾和
GBA 时期的像素插画风格。动画使用现有桌宠播放器，无需引入角色系统。

## 使用

交付物是 `output/pets/kaze-wind-ferret.zip`，由用户自行导入。
在 Kaze 顶部点击「桌宠」→「导入桌宠 ZIP 包」，选择该 ZIP。
曾进行的自动导入已撤回，当前工作区恢复原有桌宠选择。

包的源文件保存在 `assets/pets/kaze-wind-ferret/`，只有 `pet.json` 与
`spritesheet.webp` 是导入必需文件。动画预览页面位于
`output/pets/kaze-wind-ferret/preview.html`，可以切换深浅背景、暂停动画。
预览页面循环演示每种动作；真实应用会按现有规则在动作结束后回到待机。

## 动作

| 状态 | 帧数 | 动作 |
| --- | ---: | --- |
| idle | 6 | 坐姿呼吸与眨眼 |
| running-right | 8 | 向右跑动 |
| running-left | 8 | 向左跑动 |
| waving | 4 | 挥爪打招呼 |
| jumping | 5 | 蓄力、起跳、落地 |
| failed | 8 | 低头失落后恢复 |
| waiting | 6 | 抱爪等待输入 |
| running | 6 | 坐着专心忙碌 |
| review | 6 | 歪头、观察和思考 |

共 57 帧，图集 1536 × 1872，单格 192 × 208，未使用的格子保持完全透明。
动作别名 `hello`、`jump`、`rest` 分别对应挥手、跳跃、待机。

## 制作与验证

采用内置 image_gen 生成动作，以选定的透明原稿为一致性参考。
提帧、背景移除、图集组合和检查使用 hatch-pet 的确定性脚本。
左右跑动分别生成；早期镜像版因原图分格边界问题已被替换。
采用整行共享视窗提帧，保留跳跃高度，避免逐帧居中消除垂直运动。

生成提示词保存在 `output/pets/kaze-wind-ferret/generation-prompts.md`；
格式验证报告和动作总览分别是同目录的 `validation.json` 与 `contact-sheet.png`。
视觉检查与运行摘要保存在 `.tmp/kaze-pet-run/qa/`。

未修改原有桌宠的文件。撤回自动导入时修正了显示／隐藏逻辑：
隐藏不再依赖后端查询，显示时找不到桌宠包会明确报错；
恢复设置时不会仅因包选择变化而自动显示已隐藏的桌宠。
