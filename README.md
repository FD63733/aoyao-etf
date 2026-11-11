# 瑶瑶指数强度量化全览 V42 · 家庭共享自动更新版

**请先阅读 `使用说明_先看这里.txt`。**

## 快速上手

1. 注册/登录 GitHub，创建**公开仓库**（Public），例如 `yaoyao-etf`。
2. **将本压缩包解压后的内容放在仓库根目录**。必须包括 `index.html`、`scripts/update_market.py`、`.github/workflows/update-market.yml`、`.github/workflows/publish-site.yml`。Mac Finder 按 `Command+Shift+.` 可显示隐藏 `.github` 文件夹。
3. 打开仓库 `Settings` → `Pages` → `Source` → 选择 **GitHub Actions**。
4. 打开 `Actions` → `每日 ETF 共享行情更新` → `Run workflow` 手动首次运行。首次可能失败，因为免费公开行情接口可能无法从 GitHub 服务器访问。
5. 待 `Actions` 成功部署后，从 `Settings` → `Pages` 复制网址分享家人。按需从 Pages 的 `data/manifest.json` 查看实际覆盖率。

## 设计原则

- 无须付费市场数据 API Token，无须购买服务器。GitHub 免费账号的 **公开仓库**适用 GitHub Pages，公开仓库 GitHub Actions 分钟免费。未来政策可能变化。
- 仅展示实际获取的ETF及日K；绝不虚构交易价格、时间戳或假称全市场已全部扫描。
- GitHub Actions 在北京时间工作日 17:27～23:27 分批执行（每个工作日7次），每次最多160只ETF，**并不是保证每天都成功**，可能会受免费第三方接口的限流、上游调整和 GitHub 定时任务延迟影响。
- 数据自动更新器公开部署静态行情快照；所有家人在浏览器打开同一份公开数据。
- 来源是第三方公开接口，使用和再分发应遵守各数据提供方的许可和使用条款；仅用于学习/家庭参考，不作为买卖指令。
- 本项目尚未连接真实在线行情测试；须以上线后 `Actions` 日志、`data/manifest.json` 为实际依据。

## 自动任务文件

- `.github/workflows/publish-site.yml`：网页上传/修改后自动发布静态网页；若 Pages 尚未启用，先启用后重新运行。
- `.github/workflows/update-market.yml`：工作日分批获取实际ETF和指数日K，合并旧数据并发布。
- `scripts/update_market.py`：数据校验/增量恢复/历史日K写盘。

> 旧 Netlify URL 不会因为创建 GitHub Pages 而自动切换；新网址形如 `https://用户名.github.io/yaoyao-etf/`。要沿用原 Netlify 网址，需要后续配置跨域共享快照路径。
