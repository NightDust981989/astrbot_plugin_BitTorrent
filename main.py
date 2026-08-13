import re
import base64
import urllib.parse
from typing import List
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core import AstrBotConfig
import astrbot.api.message_components as Comp

# ========== 1. 配置映射类（从配置文件读取参数） ==========
@dataclass
class MagnetConfig:
    """磁力搜索配置类"""
    base_url: str          # 站点基础地址
    search_path: str       # 搜索接口路径
    max_results: int       # 最大返回结果数
    request_timeout: int   # 请求超时时间（秒）

    def __post_init__(self):
        # 处理base_url结尾的/（统一格式：不带结尾/）
        if self.base_url.endswith("/"):
            self.base_url = self.base_url.rstrip("/")
        # 处理search_path开头的/（统一格式：带开头/）
        if not self.search_path.startswith("/"):
            self.search_path = f"/{self.search_path}"

# ========== 2. 核心工具类 ==========
class MagnetUtils:
    @staticmethod
    def decrypt_base64(encrypted_str: str) -> str:
        """Base64解密"""
        try:
            encrypted_str = encrypted_str.ljust(len(encrypted_str) + (4 - len(encrypted_str) % 4) % 4, '=')
            decoded = base64.b64decode(encrypted_str).decode('utf-8', errors='ignore')
            return urllib.parse.unquote(decoded)
        except Exception as e:
            logger.warning(f"Base64解密失败：{str(e)}")
            return ""

    @staticmethod
    def get_full_url(base_url: str, relative_url: str) -> str:
        """拼接完整URL"""
        if relative_url.startswith("http"):
            return relative_url
        if relative_url.startswith("./"):
            return f"{base_url}/{relative_url[2:]}"
        if relative_url.startswith("/"):
            return f"{base_url}{relative_url}"
        return f"{base_url}/{relative_url}"
    
    @staticmethod
    def encode_search_word(keyword: str) -> str:
        """搜索关键词匹配站点加密逻辑"""
        return base64.urlsafe_b64encode(keyword.encode("utf-8")).decode("ascii").rstrip("=")

    @staticmethod
    def extract_atob_blob(html: str) -> str | None:
        """提取页面中 window.atob("...") 加密内容"""
        m = re.search(r'window\.atob\(\s*["\']([^"\']+)["\']', html)
        return m.group(1) if m else None

    @staticmethod
    def get_sort_param(sort_keyword: str) -> str:
        """
        排序关键词
        """
        sort_mapping = {
            "相关度": "rele",
            "大小": "length",
            "文件大小": "length",
            "热门": "hits",
            "热门程度": "hits",
            "时间": "time",
            "最新": "time",
        }
        # 提高鲁棒性
        sort_keyword = sort_keyword.strip().lower()
        for key, value in sort_mapping.items():
            if key.lower() == sort_keyword:
                return value
        # 匹配不到则回退默认
        return "rele"

# ========== 3. 核心搜索服务 ==========
class WhatsLinkService:
    """whatslink.info 预览服务"""

    API_URL = "https://whatslink.info/api/v1/link"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def get_preview(self, magnet_url: str) -> dict | None:
        """查询磁链预览信息，失败时返回 None"""
        try:
            resp = await self.client.get(self.API_URL, params={"url": magnet_url})
            if resp.status_code != 200:
                logger.warning(f"whatslink API 返回 {resp.status_code}")
                return None
            return resp.json()
        except Exception as e:
            logger.warning(f"whatslink API 请求失败：{e}")
            return None


class MagnetSearchService:
    def __init__(self, config: MagnetConfig, client: httpx.AsyncClient):
        self.config = config
        self.client = client

    async def search(self, keyword: str, sort_param: str = "rele") -> List[dict]:
        """搜索逻辑：返回包含原始数据的 dict 列表"""
        results = []

        try:
            # ========== 构造搜索URL ==========
            encoded_word = MagnetUtils.encode_search_word(keyword)
            search_url = f"{self.config.base_url}{self.config.search_path}?word={encoded_word}&sort={sort_param}"
            logger.debug(f"GET请求：{search_url}")

            # 发起请求
            response = await self.client.get(search_url)
            logger.debug(f"响应状态码：{response.status_code}")

            # ========== 提取并解密搜索结果块（window.atob + decodeURIComponent） ==========
            blob = MagnetUtils.extract_atob_blob(response.text)
            if not blob:
                logger.error("未找到搜索结果加密块，网站结构可能已变更")
                return []

            decrypted_html = MagnetUtils.decrypt_base64(blob)
            soup = BeautifulSoup(decrypted_html, "lxml")
            result_container = soup.find("ul", id="Search_list_wrapper")
            if not result_container:
                logger.error("网站不可用")
                return []

            detail_links = []
            # 遍历结果：最多取配置的max_results条（只取直接子 li，排除分页）
            for idx, li in enumerate(result_container.find_all("li", recursive=False)):
                if idx >= self.config.max_results:
                    break

                title_a = li.find("a", class_="SearchListTitle_result_title")
                if not title_a:
                    continue
                href = title_a.get("href", "").strip()
                if not href:
                    continue
                title = title_a.get_text(strip=True) or f"搜索结果{idx+1}"

                info = li.find("div", class_="Search_list_info")
                info_text = info.get_text(" ", strip=True) if info else ""
                size = re.search(r"文件大小：\s*([0-9.]+\s*[GMK]B)", info_text)
                size = size.group(1).strip() if size else "未知大小"
                create_time = re.search(r"创建时间：\s*(\d{4}-\d{2}-\d{2})", info_text)
                create_time = create_time.group(1).strip() if create_time else "未知时间"

                detail_links.append({
                    "url": MagnetUtils.get_full_url(self.config.base_url, href),
                    "title": title,
                    "size": size,
                    "create_time": create_time
                })

            if not detail_links:
                return []

            # ========== 解析详情页提取磁力链接 ==========
            for link in detail_links:
                try:
                    detail_resp = await self.client.get(link["url"])
                    magnet_link = None

                    detail_blob = MagnetUtils.extract_atob_blob(detail_resp.text)
                    if detail_blob:
                        detail_html = MagnetUtils.decrypt_base64(detail_blob)
                        detail_soup = BeautifulSoup(detail_html, "lxml")
                        magnet_a = detail_soup.find("a", class_="Information_magnet")
                        if magnet_a:
                            magnet_link = magnet_a.get("href", "").strip() or None
                        if not magnet_link:
                            magnet_match = re.search(r"magnet:\?xt=urn:btih:[a-fA-F0-9]{40,}[^\"']*", detail_html)
                            if magnet_match:
                                magnet_link = magnet_match.group().strip()

                    results.append({
                        "title": link["title"],
                        "magnet_link": magnet_link,
                        "size": link["size"],
                        "create_time": link["create_time"],
                    })
                except Exception as e:
                    results.append({
                        "title": link["title"],
                        "magnet_link": None,
                        "size": link["size"],
                        "create_time": "",
                        "error": str(e)[:30],
                    })

        except Exception as e:
            logger.error(f"搜索异常：{str(e)}")
            results = [{"error": f"搜索失败：{str(e)[:50]}"}]

        return results

# ========== 4. 工具函数 ==========
def _format_size(size_bytes) -> str:
    """将字节数格式化为可读大小"""
    if not size_bytes or size_bytes <= 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"

# ========== 5. 插件主类 ==========
@register(
    "astrbot_plugin_BitTorrent",
    "NightDust981989",
    "BitTorrent磁力搜索",
    "1.5.0",
    "https://github.com/NightDust981989/astrbot_plugin_BitTorrent"
)
class MagnetSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config        
        # ========== 从插件配置文件读取参数 ==========
        magnet_config_dict = self.config.get("magnet_search", {})

        base_url = magnet_config_dict.get("base_url", "https://clg38.xyz")
        search_path = magnet_config_dict.get("search_path", "/search")
        max_results = int(magnet_config_dict.get("max_results", 3))
        request_timeout = int(magnet_config_dict.get("request_timeout", 15))
        self.enable_preview = magnet_config_dict.get("enable_preview", True)

        # 初始化配置类
        self.magnet_config = MagnetConfig(
            base_url=base_url,
            search_path=search_path,
            max_results=max_results,
            request_timeout=request_timeout
        )

        # 统一创建 HTTP 客户端
        magnet_headers = {
            "User-Agent": "Mozilla/5.0 (Linux; U; Android 2.2; en-us; Droid Build/FRG22D) AppleWebKit/533.1 (KHTML, like Gecko) Version/4.0 Mobile Safari/533.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": self.magnet_config.base_url,
            "Referer": self.magnet_config.base_url
        }
        self._magnet_client = httpx.AsyncClient(
            headers=magnet_headers,
            timeout=self.magnet_config.request_timeout,
            follow_redirects=False,
            verify=False
        )
        self._preview_client = httpx.AsyncClient(
            timeout=request_timeout,
            follow_redirects=True
        )

        self.search_service = MagnetSearchService(self.magnet_config, self._magnet_client)
        self.whatslink_service = WhatsLinkService(self._preview_client)
        logger.info(f"磁力搜索插件初始化完成，使用站点：{base_url}{search_path}")

    async def terminate(self):
        await self._magnet_client.aclose()
        await self._preview_client.aclose()

    @staticmethod
    def _format_result(idx: int, res: dict) -> str:
        """格式化单条搜索结果"""
        if "error" in res and not res.get("magnet_link"):
            title = res.get("title", "未知")
            size = res.get("size", "未知")
            return f"‎\n===== 结果 {idx} =====\n‎标题：{title}\n解析失败：{res['error']}\n文件大小：{size}"

        return (
            f"‎\n===== 结果 {idx} =====\n‎"
            f"标题：{res['title']}\n"
            f"磁力链接：{res['magnet_link'] or '未提取到'}\n"
            f"文件大小：{res['size']}\n"
            f"收录时间：{res['create_time']}"
        )

    @filter.command("bt")
    async def magnet_search_handler(self, event: AstrMessageEvent):
        """
        磁力搜索指令
        使用方式：bt （排序方式） [关键词]
        示例：bt 安达与岛村 / bt 热门 安达与岛村
        """
        message = event.message_str.strip()
        args = message.split()

        chain = []

        if len(args) < 2 or args[0] != "bt":
            chain.append(Comp.Plain("用法：bt （排序方式） [关键词]\n示例：bt 热门 安达与岛村"))
            yield event.chain_result(chain)
            return

        # 解析排序参数和关键词
        sort_keyword = ""
        if len(args) == 2:
            keyword = args[1]
        else:
            sort_keyword = args[1]
            keyword = " ".join(args[2:])

        sort_param = MagnetUtils.get_sort_param(sort_keyword)
        results = await self.search_service.search(keyword, sort_param)

        if not results:
            chain.append(Comp.Plain("未找到相关磁力链接，网站失效或网络问题"))
        elif len(results) == 1 and results[0].get("error") and not results[0].get("title"):
            # 搜索整体失败
            chain.append(Comp.Plain(results[0]["error"]))
        else:
            # 第一次发送：所有基础结果 + 预览文本
            text_chain = [Comp.Plain(f"共找到 {len(results)} 条有效结果：")]
            preview_nodes = []
            for idx, res in enumerate(results, 1):
                text_chain.append(Comp.Plain(self._format_result(idx, res)))
                preview = None
                if self.enable_preview and res.get("magnet_link"):
                    preview = await self.whatslink_service.get_preview(res["magnet_link"])
                if preview:
                    text_chain.append(Comp.Plain(
                        f"‎\n--- 预览 ---\n‎"
                        f"类型：{preview.get('file_type') or '未知'}\n"
                        f"文件数：{preview.get('count') or '未知'}"
                    ))
                    # 收集截图到 Node
                    if preview.get("screenshots"):
                        screenshot_url = preview["screenshots"][0].get("screenshot")
                        if screenshot_url:
                            node_content = [Comp.Plain(f"结果{idx}预览图"), Comp.Image.fromURL(screenshot_url)]
                        else:
                            node_content = [Comp.Plain(f"结果{idx}无预览图")]
                    else:
                        node_content = [Comp.Plain(f"结果{idx}无预览图")]
                else:
                    node_content = [Comp.Plain(f"结果{idx}无预览图")]
                preview_nodes.append(Comp.Node(
                    content=node_content,
                    name=event.get_sender_name(),
                    uin=str(event.get_sender_id()),
                ))
            if self.enable_preview:
                text_chain.append(Comp.Plain("‎\n预览数据来源：whatslink.info"))
            yield event.chain_result(text_chain)

            # 第二次发送：预览图合并转发
            if self.enable_preview and preview_nodes:
                yield event.chain_result([Comp.Nodes(nodes=preview_nodes)])

    @filter.command("btp")
    async def magnet_preview_handler(self, event: AstrMessageEvent):
        """
        磁链预览指令
        使用方式：btp [磁力链接]
        示例：btp magnet:?xt=urn:btih:xxxx
        """
        message = event.message_str.strip()
        args = message.split(maxsplit=1)

        chain = []

        if len(args) < 2:
            chain.append(Comp.Plain("用法：btp [磁力链接]\n示例：btp magnet:?xt=urn:btih:xxxx"))
            yield event.chain_result(chain)
            return

        magnet_url = args[1].strip()
        if not magnet_url.startswith("magnet:"):
            chain.append(Comp.Plain("请输入有效的磁力链接（以 magnet: 开头）"))
            yield event.chain_result(chain)
            return

        preview = await self.whatslink_service.get_preview(magnet_url)
        if not preview or not preview.get("name"):
            chain.append(Comp.Plain("未查询到预览信息，链接可能无效或 API 暂不可用"))
            yield event.chain_result(chain)
            return

        text = (
            f"文件名：{preview.get('name', '未知')}\n‎"
            f"类型：{preview.get('file_type') or '未知'}\n"
            f"总大小：{_format_size(preview.get('size', 0))}\n"
            f"文件数：{preview.get('count') or '未知'}\n"
            f"‎\n预览数据来源：whatslink.info"
        )
        chain.append(Comp.Plain(text))

        if preview.get("screenshots"):
            screenshot_url = preview["screenshots"][0].get("screenshot")
            if screenshot_url:
                chain.append(Comp.Image.fromURL(screenshot_url))

        yield event.chain_result(chain)

    @filter.llm_tool(name="search_magnet")
    async def search_magnet(self, event: AstrMessageEvent, keyword: str, sort: str = "") -> MessageEventResult:
        '''搜索磁力链接资源，帮你寻找电影、软件、学习资料等。

        Args:
            keyword(string): 搜索关键词，例如"安达与岛村"
            sort(string): 排序方式，可选值：热门、时间、大小、相关度。默认按相关度排序
        '''
        sort_param = MagnetUtils.get_sort_param(sort)
        results = await self.search_service.search(keyword, sort_param)

        if not results:
            yield event.plain_result("未找到相关磁力链接，网站失效或网络问题")
        elif len(results) == 1 and results[0].get("error") and not results[0].get("title"):
            yield event.plain_result(results[0]["error"])
        else:
            # 第一次发送：所有基础结果 + 预览文本
            text_chain = [Comp.Plain(f"共找到 {len(results)} 条有效结果：")]
            preview_nodes = []
            for idx, res in enumerate(results, 1):
                text_chain.append(Comp.Plain(self._format_result(idx, res)))
                preview = None
                if self.enable_preview and res.get("magnet_link"):
                    preview = await self.whatslink_service.get_preview(res["magnet_link"])
                if preview:
                    text_chain.append(Comp.Plain(
                        f"‎\n--- 预览 ---\n‎"
                        f"类型：{preview.get('file_type') or '未知'}\n"
                        f"文件数：{preview.get('count') or '未知'}"
                    ))
                    # 收集截图到 Node
                    if preview.get("screenshots"):
                        screenshot_url = preview["screenshots"][0].get("screenshot")
                        if screenshot_url:
                            node_content = [Comp.Plain(f"结果{idx}预览图"), Comp.Image.fromURL(screenshot_url)]
                        else:
                            node_content = [Comp.Plain(f"结果{idx}无预览图")]
                    else:
                        node_content = [Comp.Plain(f"结果{idx}无预览图")]
                else:
                    node_content = [Comp.Plain(f"结果{idx}无预览图")]
                preview_nodes.append(Comp.Node(
                    content=node_content,
                    name=event.get_sender_name(),
                    uin=str(event.get_sender_id()),
                ))
            if self.enable_preview:
                text_chain.append(Comp.Plain("‎\n预览数据来源：whatslink.info"))
            yield event.chain_result(text_chain)

            # 第二次发送：预览图合并转发
            if self.enable_preview and preview_nodes:
                yield event.chain_result([Comp.Nodes(nodes=preview_nodes)])

    @filter.llm_tool(name="preview_magnet")
    async def preview_magnet(self, event: AstrMessageEvent, magnet_url: str) -> MessageEventResult:
        '''查询磁力链接的详细信息，包括文件名、类型、总大小、文件数量和预览截图。

        Args:
            magnet_url(string): 磁力链接，以magnet:?xt=urn:btih:开头
        '''
        if not magnet_url.startswith("magnet:"):
            yield event.plain_result("请输入有效的磁力链接（以 magnet: 开头）")
            return

        preview = await self.whatslink_service.get_preview(magnet_url)
        if not preview or not preview.get("name"):
            yield event.plain_result("未查询到预览信息，链接可能无效或 API 暂不可用")
        else:
            text = (
                f"文件名：{preview.get('name', '未知')}\n‎"
                f"类型：{preview.get('file_type') or '未知'}\n"
                f"总大小：{_format_size(preview.get('size', 0))}\n"
                f"文件数：{preview.get('count') or '未知'}\n"
                f"‎\n预览数据来源：whatslink.info"
            )
            chain = [Comp.Plain(text)]
            if preview.get("screenshots"):
                screenshot_url = preview["screenshots"][0].get("screenshot")
                if screenshot_url:
                    chain.append(Comp.Image.fromURL(screenshot_url))
            yield event.chain_result(chain)