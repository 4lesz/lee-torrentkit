# torrentkit

作者：**lee**

纯 Python、零第三方依赖的 BitTorrent 工具包：**解析 `.torrent` → 生成磁力链接 → 用磁力链接拉取元数据并下载**。自带一个最小可用的 BT 协议实现，以及一个本地做种器（用于局域网分享和端到端测试）。

## 项目说明

torrentkit 是一个面向学习和个人使用的种子 / 磁力下载工具，不依赖 libtorrent 等第三方库，所有协议都用 Python 标准库实现。它提供三种用法：

- **Web 界面**（推荐）：浏览器里拖入种子或粘贴磁力链接，查看文件列表、创建下载任务、看进度；
- **命令行**：`torrentkit info / magnet / download / create / serve / web`；
- **Python API**：在自己的脚本里解析种子、生成磁力链接、下载或做种。

目录结构：

| 路径 | 说明 |
| --- | --- |
| `start.bat` / `start.sh` | 一键启动脚本（Windows / macOS、Linux） |
| `torrentkit/bencode.py` | bencode 编解码 |
| `torrentkit/metainfo.py` | `.torrent` 解析、info hash、磁力链接生成与解析 |
| `torrentkit/create.py` | 创建种子 |
| `torrentkit/tracker.py` | HTTP / UDP tracker |
| `torrentkit/dht.py` | DHT 查找 peer |
| `torrentkit/peers.py`、`sockets.py` | Peer Wire Protocol、扩展协议（ut_metadata / ut_pex） |
| `torrentkit/pieces.py`、`storage.py` | 分片调度、校验与磁盘读写 |
| `torrentkit/download.py` | 下载器（整合 tracker / DHT / PEX / peer） |
| `torrentkit/seeder.py` | 本地做种器 |
| `torrentkit/web.py`、`webui/` | 本地 Web UI 服务端与前端页面 |
| `torrentkit/cli.py` | 命令行入口 |
| `tests/` | 单元测试与本地端到端测试 |

## 下载

- 不会用 git：在仓库页面点绿色的 **Code** 按钮 → **Download ZIP**，解压即可。
- 会用 git：

```bash
git clone https://github.com/4lesz/lee-torrentkit.git
```

## 一键启动

只需要安装 Python 3.9+（[下载地址](https://www.python.org/downloads/)，Windows 安装时勾选「Add Python to PATH」），无需 `pip install` 任何依赖。

- **Windows**：双击项目根目录下的 `start.bat`。
- **macOS / Linux**：在项目目录执行 `./start.sh`（首次可能需要 `chmod +x start.sh`）。

脚本会自动找到 Python、创建 `downloads` 目录、启动 Web UI 并在浏览器打开 <http://127.0.0.1:8080/>；下载的文件默认保存在 `downloads/`。关闭窗口或按 `Ctrl+C` 即可停止。端口被占用时可以换一个端口：

```powershell
.\start.bat 9000
```

```bash
./start.sh 9000
```

## 功能

- `bencode` 编解码（严格校验，错误输入直接拒绝）
- `.torrent` 解析：v1、v2 文件树、hybrid，计算 v1 SHA-1 / v2 SHA-256 info hash
- 磁力链接生成与解析：`btih`（hex / base32）、`btmh`、`dn`、`xl`、`tr`、`ws`
- Tracker：HTTP(S)（BEP 3 / BEP 23 compact peers）与 UDP（BEP 15）
- Peer Wire Protocol（BEP 3）：16 KiB 块流水线、SHA-1 分片校验、多 peer 并发
- 断点续传：启动时校验本地已有文件，只补齐缺失分片
- `ut_metadata`（BEP 9 / BEP 10）：从磁力链接向 peer 拉取 torrent 元数据
- DHT（BEP 5）：无 tracker 时自动查找 peer、向网络宣告自己；私有种子自动关闭
- PEX（BEP 11 `ut_pex`）：与已连接的 peer 互换 peer 列表；私有种子不启用（BEP 27）
- 本地做种器：只提供本地已校验的分片
- 本地 Web UI：拖拽解析、磁力预览、任务队列、下载进度、停止任务、打开目录
- CLI + Python API + 测试（本地 tracker / 本地 seeder / 真实 TCP 下载）

## 安装

要求 Python 3.9+。

```powershell
python -m pip install -e .
torrentkit --help
```

也可以不安装，直接运行：

```powershell
python -m torrentkit --help
```

## 快速开始

```powershell
# 1. 查看种子详情（文件列表、分片、tracker、info hash）
torrentkit info .\example.torrent

# 2. 由种子生成磁力链接
torrentkit magnet .\example.torrent

# 3. 从种子文件下载到指定目录
torrentkit download .\example.torrent -d .\downloads

# 4. 从磁力链接下载（磁力里带 tracker 时会自动获取元数据）
torrentkit download "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&tr=udp://tracker.example:6969" -d .\downloads

# 5. 手动指定 peer（例如局域网内的做种机，或者用 --no-dht 关闭 DHT 时）
torrentkit download .\example.torrent -d .\downloads --peer 192.168.1.10:6881

# 6. 创建种子
torrentkit create .\my-folder --piece-length 256KiB `
  --tracker udp://tracker.opentrackr.org:1337/announce -o my-folder.torrent

# 7. 本地做种（仅提供已通过哈希校验的分片）
torrentkit serve my-folder.torrent -d .
```

`download` 的常用参数：

| 参数 | 说明 |
| --- | --- |
| `-d, --dir` | 下载目录（默认当前目录） |
| `--port` | 向 tracker 宣告的端口（默认 6881） |
| `--peer HOST:PORT` | 追加直连 peer，可重复 |
| `--max-peers` | 最大并发连接数（默认 100；很多网络按连接限速，连接越多总速度越高） |
| `--rarest-first` | 按稀有度挑分片（默认按分片索引顺序） |
| `--no-dht` | 不通过 DHT 查找 peer（私有种子总是不用 DHT） |
| `-v` | 打开调试日志 |

## Web 界面

```powershell
python -m torrentkit web -d .\downloads --port 8080 --max-active 2 --open
```

然后打开 <http://127.0.0.1:8080/>。页面支持：

- 拖拽 `.torrent` 或粘贴磁力链接，显示名称、大小、分片数、info hash、文件列表和磁力链接
- 一键复制磁力链接
- 创建下载任务，实时显示进度、速度、连接数、分片完成度和剩余时间
- 任务队列：`--max-active` 控制并发下载数（默认 2），其余任务显示「排队中」和队列位置
- 下载目录可选择：直接输入绝对路径，或点「浏览…」在服务端文件夹浏览器里选择/新建目录；选择会记住
- 准备/校验阶段单独显示状态与进度，不再一律显示「排队中」
- 按需分配文件，不会在开始下载前按种子总大小占满磁盘
- 相同 info hash 的任务自动去重；准备、校验、下载阶段都可以停止
- 打开下载目录、从列表移除任务
- 高级选项：直连 peer、最大连接数、稀有分片优先、DHT 自动发现开关
- 运行中的任务可以补充 Peer / Tracker，新 Tracker 会立即宣告

安全说明：Web UI 默认只绑定 `127.0.0.1`，没有登录认证，不要用 `--host 0.0.0.0` 暴露到不可信网络。任务列表只保存在内存中，重启页面服务后历史任务会消失，但已下载文件仍在磁盘上。默认在回环绑定时允许选择任意本地下载目录；用 `--restrict-dirs` 可以限制为 `--dir` 内部（绑定非回环地址时默认就是限制的）。

### 本地演示种子

仓库里可以生成一份完全本地的演示数据（不会提交到 git）：

```powershell
python -m torrentkit create .\demo\demo-content --piece-length 32KiB -o .\demo\demo-content.torrent
python -m torrentkit serve .\demo\demo-content.torrent -d .\demo --host 127.0.0.1 --port 6881
```

然后在页面拖入 `demo\demo-content.torrent`，在高级选项的直连 peer 里填 `127.0.0.1:6881`，即可完整走一遍「解析 → 准备 → 校验 → 下载 → 完成」。

### 一直显示「下载中」但没有速度？

- 客户端从 **Tracker**、**DHT**、**PEX** 和 **直连 Peer** 四个来源找节点。DHT 需要先连上引导节点（`router.bittorrent.com` 等），冷启动通常要几秒到几十秒。
- 种子没有 Tracker 且关闭了 DHT（或是私有种子）时，必须在高级选项里填写直连 Peer；否则任务会直接报错，而不是无限等待。
- 如果任务显示「等待 Peer」，卡片下方会给出具体原因：没有 Tracker、Tracker 全部失败，或 Tracker 返回 0 个 Peer。
- DHT 走 UDP；如果所在网络屏蔽 UDP，DHT 和 UDP tracker 都会失效。
- 检查防火墙是否阻止了出站连接，以及 Tracker 是否仍然存活。
- 本地演示种子没有 Tracker，公网 DHT 上也不会有它，只能用 `127.0.0.1:6881` 作为 Peer。

## Python API

```python
from torrentkit import Torrent
from torrentkit.download import Downloader

torrent = Torrent.from_file("example.torrent")
print(torrent.name, torrent.total_length)
print(torrent.magnet_uri())          # 生成磁力链接

downloader = Downloader(torrent, "./downloads", extra_peers=[("192.168.1.10", 6881)])
downloader.run()                     # 阻塞直到完成，返回 True/False
```

磁力链接 → 下载：

```python
from torrentkit.download import Downloader, resolve_source

torrent, magnet = resolve_source(
    "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&tr=udp://tracker.example:6969"
)
Downloader(torrent, "./downloads").run()
```

本地做种：

```python
from torrentkit import Torrent
from torrentkit.seeder import LocalSeeder

torrent = Torrent.from_file("example.torrent")
seeder = LocalSeeder(torrent, "./", host="0.0.0.0", port=6881)
port = seeder.start()
print("seeding on", port)
# seeder.stop()
```

## 协议支持范围

| 能力 | 状态 |
| --- | --- |
| bencode / `.torrent` v1 | 支持 |
| v2 文件树解析 / v2 info hash | 支持（仅解析与生成磁力） |
| hybrid（v1 + v2） | 支持（走 v1 分片下载） |
| HTTP(S) tracker、compact peers | 支持 |
| UDP tracker（BEP 15） | 支持 |
| Peer Wire Protocol（BEP 3） | 支持（下载方向） |
| `ut_metadata`（BEP 9/10） | 支持 |
| 断点续传 / 本地数据校验 | 支持 |
| 任务队列 / 并发限制 | 支持（Web UI，默认 2 个并发） |
| 本地做种 | 支持（`serve`） |
| 本地 Web UI | 支持（`web`，默认仅回环地址） |
| DHT（BEP 5） | 支持（仅 IPv4；查找 peer、announce、应答基础查询） |
| PEX（BEP 11） | 支持（增量交换 IPv4/IPv6 peer） |
| 私有种子（BEP 27） | 支持（自动禁用 DHT 与 PEX） |
| uTP、MSE/加密传输 | 不支持 |
| 纯 v2 种子下载 | 不支持 |
| Web Seed 实际下载 | 不支持（仅解析并写入磁力） |
| Endgame 请求、选择性下载 | 不支持 |

## 实现说明

- 分片以 16 KiB 为块请求，单 peer 默认 8 个在途请求；多个 peer 并行拉取不同分片。
- 每块写入前都会校验整片 SHA-1，校验失败会释放该分片并断开该 peer。
- 每个 peer 独占一个分片（不做块级共享），实现简单但吞吐不如 libtorrent；适合中小文件、局域网自测和学习协议。
- 启动时会重新校验下载目录里的数据，大种子耗时较长；这是实现可靠续传的代价。文件按需分配，不会预先按总大小占满磁盘；校验和准备阶段可以被停止。
- Web 任务队列默认最多同时下载 2 个任务（`--max-active`），其余任务排队；相同 info hash 的任务会被拒绝，避免重复下载同一目录。
- 下载器只发起出站连接，不接受入站连接；需要做种请用 `serve` 子命令。
- DHT 节点是精简实现：只为当前 info hash 维护最多 200 个节点，不实现完整的 k-bucket 路由表，只处理 IPv4；不是本节点发出的查询所对应的回复会被直接丢弃。
- 下载器只发起出站连接、不接受入站连接，所以通过 DHT 宣告的端口通常连不进来；它主要是帮自己找 peer。
- PEX 每分钟最多向同一 peer 发一次，只发送对方还没收到过的 peer。

## 安全与合规

- 解析时拒绝 `..`、路径分隔符等目录穿越写法，文件只会落在下载目录内。
- 只校验分片哈希，不校验文件来源；不要执行种子带来的可执行文件。
- 本项目是一个通用协议工具，BitTorrent 本身合法，但请只下载你有权分发/获取的内容。用它下载未经授权的版权作品可能违法。

## 测试

```powershell
python -m unittest discover -s tests -t .
```

测试覆盖 bencode、metainfo、v2 文件树、磁力链接、存储分片映射、HTTP/UDP tracker、本地 peer 下载、磁力链接元数据交换、CLI，以及 Web UI 的解析、任务下载、队列/并发、去重、停止和移除流程；DHT（两个本地节点互查/宣告、伪造回复与 token、只靠 DHT 完成下载）和 PEX（与模拟 peer 互换列表、私有种子不启用）也都在本地回环上测试。
