# pi 模型配置工具

*纯Ai做的，Ai上传的，我是技术小白，只会口喷Ai！*

<img width="1477" height="965" alt="image" src="https://github.com/user-attachments/assets/62dc5522-8ab1-408d-a829-2e6c827de69c" />

<img width="1400" height="900" alt="image" src="https://github.com/user-attachments/assets/f834867d-034e-4f71-8050-0eb774200741" />

给 pi（AI 编程助手）用户做的 Windows 桌面小工具：填入第三方中转的地址和密钥，自动拉取模型列表、匹配官方参数（上下文长度、思考等级、图片输入、兼容开关），一键合并写入 models.json。还支持批量删除失效的服务商和模型、直接修改已配置模型的参数，以及逐模型实测可用性、延迟、协议匹配、图片识别和上下文抽查。

写入前会按 pi 的规则自检，本机装了 pi 还会调用 pi 自己的校验器复核并在临时沙盒里让 pi 试读一遍，写完立刻用 pi --list-models 确认结果，出问题可一键还原备份。因为 pi 读配置是全有全无的：一处字段非法就会让整份 models.json 作废。

针对第三方中转的几个要点工具都替你处理了：协议不同的模型写在同一个服务商里（pi 允许模型自带接口格式和地址，不必拆成两个条目）；默认补上中转常需要的兼容项（pi 会把未知中转当成标准 OpenAI，发 developer 角色和 max_completion_tokens，多数中转两样都不认）；实测默认串行加间隔，避免触发中转的反探测封禁；中转声称支持 Claude 接口但实际返回 OpenAI 格式时会直接判出来并给一键修复。

双击即用的单文件 exe，无需安装 Python。

## 文档

- 使用说明.md：面向使用者的操作手册
- CHANGELOG.md：每版改了什么

## 快速开始

1. 从 Releases 下载 pi模型配置工具.exe，双击运行（首次会有 SmartScreen 提示，点“更多信息”再“仍要运行”）。
2. 填服务商标识、接口地址、密钥，点拉取模型列表。
3. 勾选要用的模型，预览后写入。写入前自动备份原配置，写入后自动用 pi 复核。
4. 在 pi 里按 /model 即可看到新模型。

## 注意事项

- 需要 Windows 10/11 64 位。
- 本机装有 Node.js 时体验最佳（部分中转的防护墙只放行 Node 客户端）；没有也能用，个别中转会拉取失败。
- 工具只在本地读写你自己的 ~/.pi/agent/models.json，密钥只会发送到你填写的中转地址，没有任何联网上报。
- snapshot 文件夹里的官方参数库快照来自 @earendil-works/pi-ai（MIT 许可），运行时会优先读取本机 pi 安装目录的实时数据，快照仅作兜底，由 CI 每周自动更新。

## 从源码构建

需要 Windows 10/11 和 Python 3.12+：

```
python -m venv venv
venv/Scripts/pip install pyinstaller==6.22.2
venv/Scripts/python -m PyInstaller --onefile --windowed --clean --noconfirm --name "pi模型配置工具" --add-data "snapshot;snapshot" --add-data "tests;tests" --add-data "nodefetch.mjs;." --add-data "pivalidate.mjs;." main.py
```

产物在 dist 文件夹。构建前先跑测试：

```
python -m unittest discover -s tests -t .
```

打包后再跑一次 `dist/pi模型配置工具.exe --selftest`，退出码 0 才算通过。

也可以直接打 tag 交给 GitHub Actions 构建发布：

```
git tag v2.0
git push origin v2.0
```

## 维护脚本

`python tools/update_snapshot.py` 从 npm 拉取最新的 @earendil-works/pi-ai 并更新 snapshot 目录（加 --dry-run 只看版本号）。仓库里的定时工作流每周一自动跑一次。

`python tools/tidy_models_json.py` 整理现有的 models.json：规范地址、按官方参数库校准数值、补价格与兼容项、清理无效字段，可用 --drop 删服务商、--merge 合并同站同密钥的两个条目。默认只演练打印改动清单，加 --write 才落盘（落盘前自检、pi 校验、沙盒试读三道关，并自动备份）。

`python tools/ui_shot.py 输出.png` 起一个填了示例数据的窗口并截图，用于界面回归对照。

## 许可

MIT，详见 LICENSE。snapshot 数据来自 @earendil-works/pi-ai（MIT）。
