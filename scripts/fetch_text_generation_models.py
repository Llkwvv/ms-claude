#!/usr/bin/env python3
"""抓取指定URL下所有模型名称（支持翻页），更严格的去重和排序"""

import asyncio
import json
import sys
from playwright.async_api import async_playwright

TARGET_URL = "https://www.modelscope.cn/models?filter=inference_type&tasks=hotTask:text-generation"

async def scrape():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        )

        all_names = []

        for page_num in range(1, 5):
            page = await context.new_page()
            url = TARGET_URL if page_num == 1 else f"{TARGET_URL}&page={page_num}"
            print(f"\n--- 第 {page_num} 页: {url} ---", file=sys.stderr)

            try:
                await page.goto(url, wait_until='load', timeout=120000)
                # 等久一点确保所有卡片都渲染完
                await asyncio.sleep(6)
            except Exception as e:
                print(f"  页面加载出错: {e}", file=sys.stderr)
                await page.close()
                continue

            # 获取所有模型链接（用href包含/models/来筛选）
            links = await page.evaluate('''() => {
                const links = document.querySelectorAll('a[href*="/models/"]');
                const results = [];
                links.forEach(a => {
                    const href = a.getAttribute('href') || '';
                    // 排除导航链接（只保留模型详情链接）
                    if (href.includes('/models/') && !href.includes('page=')) {
                        results.push(href);
                    }
                });
                return results;
            }''')

            print(f"  找到 {len(links)} 个模型链接", file=sys.stderr)

            # 用Set去重本页
            page_names = []
            for link in links:
                # 从URL提取模型名称
                # 格式通常是 /models/owner/model-name
                parts = link.strip('/').split('/')
                if len(parts) >= 3 and parts[0] == 'models':
                    name = f"{parts[1]}/{parts[2]}"
                    if name not in page_names:
                        page_names.append(name)

            # 也尝试从DOM文本提取（更可靠）
            dom_names = await page.evaluate('''() => {
                const names = [];
                const titleEls = document.querySelectorAll('[class*="title"] h3, [class*="title"] a, .card-title a, .model-name, h3 a');
                titleEls.forEach(el => {
                    const text = el.innerText.trim();
                    if (text && text.includes('/')) {
                        // 只取第一行（模型ID通常在第一行）
                        const firstLine = text.split('\\n')[0].trim();
                        if (firstLine.includes('/')) {
                            names.push(firstLine);
                        }
                    }
                });
                // 如果上面没找到，用更宽泛的选择器
                if (names.length === 0) {
                    const allAs = document.querySelectorAll('a[href*="/models/"]');
                    allAs.forEach(a => {
                        const text = a.innerText.trim();
                        if (text && text.includes('/') && text.length > 3) {
                            // 只取第一行
                            const firstLine = text.split('\\n')[0].trim();
                            if (firstLine.includes('/')) {
                                names.push(firstLine);
                            }
                        }
                    });
                }
                return names;
            }''')

            # 优先用DOM文本提取的结果
            if dom_names:
                page_names = list(dict.fromkeys(dom_names))  # 去重保序

            print(f"  本页模型 ({len(page_names)} 个):", file=sys.stderr)
            for name in page_names:
                print(f"    {name}", file=sys.stderr)

            all_names.extend(page_names)
            await page.close()

            if len(page_names) == 0:
                print(f"  第 {page_num} 页无数据，结束")
                break

            await asyncio.sleep(2)

        await browser.close()

        # 全局去重
        unique_names = list(dict.fromkeys(all_names))

        # 输出 JSON 到 stdout（供 Node.js 调用）
        print(json.dumps(unique_names, ensure_ascii=False))

        print(f"\n{'='*60}", file=sys.stderr)
        print(f"总共抓取到 {len(unique_names)} 个模型（去重后）", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        for i, name in enumerate(unique_names, 1):
            print(f"  {i:2d}. {name}", file=sys.stderr)

        return unique_names

if __name__ == '__main__':
    result = asyncio.run(scrape())
    print(f"\n最终结果: 共 {len(result)} 个模型", file=sys.stderr)
