import argparse
import asyncio
import contextlib

import json
import logging
import multiprocessing
import re
import sys
from datetime import datetime, timedelta
from time import sleep

from fastapi import FastAPI, Request
from playwright.async_api import async_playwright, BrowserContext, Page

from result import Result

browser: BrowserContext = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Lifespan Start...")
    try:
        yield
    finally:
        logging.info("Shutting down...")
        try:
            await close_page()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)


async def get_browser():
    if not browser:
        await create_page()
    return browser


def instagram_extract_post_id(url):
    # 定义正则表达式，匹配 /p/ 或 /reel/ 后的 ID
    match = re.search(r"/(?:p|reel)/([^/]+)/", url)
    if match:
        return match.group(1)
    return None


async def fb_parse(link):
    if not browser:
        await create_page()
    page = None
    try:
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1920, "height": 1080})
        await page.goto(link)

        current_url = page.url
        reel_page = '/reel/' in current_url
        if reel_page:
            post = await page.query_selector('div[data-pagelet="Reels"]')
            if post:
                post = await post.evaluate_handle("""
                 (element) => {
                     return element.parentElement;
                 }
             """)
            if not post:
                return Result.fail_with_msg(f"fb {link} parse failed")
        else:
            post = await page.query_selector('xpath=//div[@aria-posinset="1"]')
        is_reels = await post.evaluate("""
        (element) => {
            return element.querySelector('[data-pagelet="Reels"]') !== null;
        }
        """)
        if is_reels:
            avatar_url = await post.evaluate("""
            (element) => {
             const avatarImage = element.querySelector('svg[aria-label="头像"][data-visualcompletion="ignore-dynamic"] image');
                 if (avatarImage) {
                    return avatarImage.getAttribute('xlink:href');
                 }
                return null;
            }
            """)
            if reel_page:
                username = await post.evaluate("""
                        (element) => {
                            const elements = element.querySelectorAll('a[aria-label="查看所有者个人主页"]');
                            if (elements.length > 1) {
                                return elements[1].textContent.trim();  // 获取第二个元素的文本内容
                            }
                            return '';
                        }
                    """)
            else:
                username = await post.evaluate("""
                    (element)=>{
                         const divElement = Array.from(element.querySelectorAll('span')).find(span =>   span.textContent.includes('短视频') || span.textContent.includes('Reels'));
                             if (divElement) {
                               const objectElement = divElement.querySelector('object[type="nested/pressable"]');
                               if (objectElement) {
                                 const aElement = objectElement.querySelector('a');
                                 if (aElement) {
                                   return aElement.textContent.trim();
                                 }
                               }
                             }
                         return ''; 
                    }
                    """)

            post_id = await post.evaluate("""
             (element) => {
                 const videoElement = element.querySelector('div[data-video-id]');
                 if (videoElement) {
                   return videoElement.getAttribute('data-video-id');
                 }
                 return null; 
             }
            """)
            post_link = ""
            if post_id:
                post_link = f"https://www.facebook.com/reel/{post_id}"
            profile_id = await post.evaluate("""
              (element) => {
                const linkElement = element.querySelector('a[aria-label="查看所有者个人主页"]');
                if (linkElement) {
                  return linkElement.getAttribute('href');
                }
                return null;
              }
            """)
            if profile_id:
                if reel_page:
                    profile_id = f"https://www.facebook.com{profile_id.split('&')[0]}"
                else:
                    profile_id = f"https://www.facebook.com{profile_id.split('/?')[0]}"

            timestamp = await page.query_selector(
                'xpath=//span[contains(text(), "分钟") or contains(text(), "小时") or contains(text(), "天") or contains(text(), "月") or contains(text(), "年")]')
            if timestamp:
                timestamp = await timestamp.text_content()
                if timestamp:
                    timestamp = timestamp.strip()
                timestamp = await parse_relative_time(timestamp)
            post_content = await post.evaluate("""
                (element) => {
                    const reelsDiv = element.querySelector('div[data-pagelet="Reels"]');
                    if (!reelsDiv) {
                        return '';
                    }
            
                    const nextSiblingDiv = reelsDiv.nextElementSibling;
                    if (!nextSiblingDiv) {
                        return ''; 
                    }
                    return nextSiblingDiv.textContent.trim();
                }
                """)
            hashtags = await post.evaluate("""
              (element) => {
                const results = [];
                const aTags = element.querySelectorAll('a[href*="hashtag"]');
                aTags.forEach(aTag => {
                    if (aTag.textContent.startsWith('#')) {
                        results.push(aTag.textContent.trim());
                    }
                });
                return [...new Set(results)]; 
            }
            """)
        else:
            profile_id = await post.evaluate(
                """     (element) => {       
                        const profileLinkElement = element.querySelector('[data-ad-rendering-role="profile_name"] a');    
                        return profileLinkElement ? profileLinkElement.href : null;  
                } """)
            if profile_id:
                profile_id = profile_id.split('/?')[0]
                # profile_id = f"https://www.facebook.com/profile.php?id={profile_id}"
            username = await post.evaluate("""
                (element) => {
                    const profileNameElement = element.querySelector('[data-ad-rendering-role="profile_name"] span a span');
                    return profileNameElement ? profileNameElement.innerText : null;
                }
            """)
            avatar_url = await post.evaluate("""
                (element) => {
                    const svgImage = element.querySelector('svg[data-visualcompletion="ignore-dynamic"] image');
                    return svgImage ? svgImage.getAttribute('xlink:href') : null;
                }
            """)
            post_link = await post.evaluate("""
                (element) => {
                const aTag = element.querySelector('a[role="link"][href*="/posts/"]');
                return aTag ? aTag.href : null;
                }
             """)
            post_id = ""
            if post_link:
                post_link = post_link.split('/?')[0]
            if post_link:
                post_id = post_link.split('/posts/')[1]
            timestamp = await post.query_selector_all(
                'xpath=//a[contains(@aria-label, "小时") or contains(@aria-label, "分钟") or contains(@aria-label, "天") or contains(@aria-label, "月") or contains(@aria-label, "年")]')
            if timestamp:
                timestamp = await timestamp[0].get_attribute('aria-label')
            if timestamp:
                timestamp = await parse_relative_time(timestamp)
            post_content = await post.evaluate("""
            (element) => {
                const contentDiv = element.querySelector('div[data-ad-rendering-role="story_message"]');
                return contentDiv ? contentDiv.innerText : null;
            }
            """)
            hashtags = await post.evaluate("""
            (element) => {
                const contentDiv = element.querySelector('div[data-ad-rendering-role="story_message"]');
                if (!contentDiv) return [];
    
                const tags = contentDiv.querySelectorAll('a');
                let tagArray = [];
                tags.forEach(tag => {
                    if (tag && tag.href && tag.href.includes('hashtag')) {
                        tagArray.push(tag.innerText);
                    }
                });
                return tagArray;
            }
            """)
        if reel_page:
            # 获取 class 为指定值的第 3、4、5 个 div 元素
            like_count=0
            comments=0
            share=0
            div_elements = await page.query_selector_all('xpath=//div[@class="x9f619 x1n2onr6 x1ja2u2z x78zum5 xdt5ytf x2lah0s x193iq5w x1xmf6yo x1e56ztr xzboxd6 x14l7nz5"][position() >= 3 and position() <= 5]')
            for div_element in div_elements:
                aria_label_div = await div_element.query_selector('div[aria-label="赞"]')
                if aria_label_div:
                    like_count = await div_element.text_content()
                    if not like_count.strip():
                        like_count = 0
                aria_label_div = await div_element.query_selector('div[aria-label="评论"]')
                if aria_label_div:
                    comments = await div_element.text_content()
                    if not comments.strip():
                        comments = 0
                aria_label_div = await div_element.query_selector('div[aria-label="分享"]')
                if aria_label_div:
                    share = await div_element.text_content()
                    if not like_count.strip():
                        share = 0

        else:
            like_count = await post.evaluate("""
                (element) => {
                    const posts = Array.from(element.querySelectorAll('div[role="button"]'));
                    for (let post of posts) {
                        if (post.textContent.includes('所有心情：')) {
                            const siblingSpan = post.querySelector('span span');
                            if (siblingSpan && siblingSpan.textContent.trim() !== '') {
                            return siblingSpan.textContent.trim();
                            }
                        }
                    }
                    return 0;
                }
                """)
            comments = await post.evaluate("""
                (element) => {
                        const spans = Array.from(element.querySelectorAll('span'));
                        for (let span of spans) {
                        if (span.textContent.includes('条评论')) {
                            const match = span.textContent.match(/(\\d+)\\s*条评论/);
                            if (match) {
                                return match[1];
                            }
                        }
                    }
                    return 0;
                }
                """)
            share = await post.evaluate("""
            (element) => {
                    const spans = Array.from(element.querySelectorAll('span'));
                    for (let span of spans) {
                    if (span.textContent.includes('次分享')) {
                        const match = span.textContent.match(/(\\d+)\\s*次分享/);
                        if (match) {
                            return match[1];
                        }
                    }
                }
                return 0;
            }
            """)

        like_count=parse_number(like_count)
        comments=parse_number(comments)
        share=parse_number(share)
        return Result.ok({
            'avatarUrl': avatar_url,
            'username': username,
            'profileId': profile_id,
            'pushTime': timestamp,
            'postContent': post_content,
            'postLink': post_link,
            'postId': post_id,
            'hashtags': hashtags,
            'like': like_count,
            'comments': comments,
            'share': share,
        })

    except Exception as e:
        print(f"post parse exception:{e}")
        return Result.fail_with_msg(f"fb [{link}] parse failed: {e.args[0]}")
    finally:
        await page.close()

def parse_number(number_text):
    # 如果文本为空或无效，直接返回 0
    if not number_text.strip():
        return 0

    # 去掉文本中的空格和逗号
    number_text = number_text.replace(",", "").replace(" ","").strip()

    # 正则处理 '万' 的情况
    match = re.match(r"(\d+(\.\d+)?)\s?万", number_text)
    if match:
        return int(float(match.group(1)) * 10000)

    # 如果没有 '万'，直接转为整数
    try:
        return int(number_text)
    except ValueError:
        return 0

async def parse_relative_time(relative_str):
    now = datetime.now()

    time_units = {
        '分钟': 'minutes',
        '小时': 'hours',
        '天': 'days',
    }

    # 使用正则表达式提取数字和单位
    match = re.match(r'(\d+)(分钟|小时|天)', relative_str)
    if match:
        value, unit = match.groups()
        value = int(value)
        if unit in time_units:
            delta = timedelta(**{time_units[unit]: value})
            return (now - delta).strftime('%Y-%m-%d %H:%M:%S')

    match = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2}):(\d{2})', relative_str)
    if match:
        year, month, day, hour, minute = map(int, match.groups())
        try:
            return datetime(year, month, day, hour, minute).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass

    match = re.match(r'(\d{1,2})月(\d{1,2})日', relative_str)
    if match:
        month, day = map(int, match.groups())
        year = now.year
        # 如果当前月份小于给定月份，则认为是上一年
        if now.month < month or (now.month == month and now.day < day):
            year -= 1
        try:
            return datetime(year, month, day, 0, 0).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass

    return now.strftime('%Y-%m-%d %H:%M:%S')


async def instagram_parse(link):
    logging.info("instagram link parse: %s", link)
    if not browser:
        await create_page()
    page = None
    try:
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1920, "height": 1080})
        await page.goto(link)
        # 发布时间
        push_datetime = await page.locator("(//time)[last()]").get_attribute("datetime")
        push_time = datetime.strptime(push_datetime, "%Y-%m-%dT%H:%M:%S.%fZ").strftime("%Y-%m-%d %H:%M:%S")
        # 点赞数量
        likes = page.locator("(//a[span[contains(text(), 'likes')]])/span/span")
        if await likes.count() > 0:
            likes = await likes.text_content()
        else:
            likes = 0
        avatar_url = await page.locator("(//img[contains(@alt, 'profile picture')])[1]").get_attribute("src")
        username = await page.locator("(//img[contains(@alt, 'profile picture')])[1]").get_attribute("alt")
        username = username.split("'s profile picture")[0] if username else ""
        profile_url = f"https://www.instagram.com/{username}/" if username else ""
        post_link = link
        post_id = instagram_extract_post_id(post_link)
        tag_links = await page.locator("//a[contains(@href, '/explore/tags/')]").all()
        tags = set()
        for tag in tag_links:
            href = await tag.get_attribute("href")
            # 提取标签名 (即 href 中的 xxxx)
            tag_name = href.split("/explore/tags/")[1].strip("/") if href else None
            tags.add(tag_name)
        tags = list(tags)
        await page.close()
        likes=parse_number(likes)
        return Result.ok({
            "username": username,
            "profileId": username,
            "profileUrl": profile_url,
            "postLink": post_link,
            "postId": post_id,
            "tags": tags,
            "profileImage": avatar_url,
            "pushTime": push_time,
            "content": "",
            "retweets": 0,
            "likes": likes,
            "comments": 0,
        }).to_dict()
    except Exception as e:
        if page:
            await page.close()
        await close_page()
        return Result.fail_with_msg(f"instagram parse failed:{e.args[0]}")


async def fb_login(username: str, password: str):
    logging.info("fb login username [%s]", username)
    browser_ = await get_browser()
    try:
        page = await browser_.new_page()
    except Exception as e:
        global browser
        browser = None
        return Result.fail_with_msg(f"new page failed:{e.args[0]}")
    try:
        await page.set_viewport_size({"width": 1920, "height": 1080})
        # 设置页面的缩放比例，例如将页面内容缩放至90%
        await page.evaluate("() => document.body.style.zoom='90%'")
        facebook_home = "https://www.facebook.com/login/"
        logging.info(f"GO TO {facebook_home}")
        await page.goto(facebook_home)
        login_button_locator = page.locator('//button[@id="loginbutton"] | //button[@data-testid="royal_login_button"]')
        try:
            logging.info("wait dialog cookie policy")
            cookie_popup_div = page.locator(
                '//div[contains(@aria-label, "拒绝使用非必要 Cookie")] | //span[text()="Decline optional cookies"]')
            if await cookie_popup_div.count() > 0:
                logging.info("click first cookie policy choose")
                await cookie_popup_div.first.wait_for(state="visible", timeout=3000)  # 等待最多3秒
                await cookie_popup_div.first.click()
        except Exception as e:
            logging.warning("No Cookie policy", e)
        if await login_button_locator.is_visible():
            await page.wait_for_function("window.location.href.startsWith('https://www.facebook.com/login/')",
                                         timeout=6000 * 10 * 4)
            logging.info("Login Page Load normal")

            await page.fill('//input[@autocomplete="username"] | //input[@data-testid="royal_email"]', username)
            sleep(1)
            await page.fill('//input[@autocomplete="current-password"] | //input[@data-testid="royal_pass"]', password)
            sleep(1)
            await login_button_locator.click()
            await page.wait_for_load_state('load', timeout=10000)  # 10秒等待加载完成
            sleep(1)
            captcha = page.locator('//img[contains(@src, "/captcha/tfbimage")]')
            if await captcha.count() > 0:
                captcha_url = await captcha.get_attribute("src")
                logging.info(f"fb captcha:{captcha_url}")
                captcha_code = input("请输入验证码: ")
                captcha_input = page.locator('input[autocomplete="off"]')
                await captcha_input.fill(captcha_code)
                continue_button = page.locator('//span[text()="Continue"]')
                await continue_button.click()
                await page.wait_for_load_state('load', timeout=10000)  # 10秒等待加载完成

            await page.goto("https://www.facebook.com/")

        await page.wait_for_selector('input[type="search"]', timeout=10000)
        search_input = page.locator('input[type="search"]')
        if await search_input.count() > 0:
            logging.info(f"{username} login fb success")

    except Exception as e:
        return Result.fail_with_msg(f"{username} login facebook failed:{e.args[0]}")
    finally:
        await page.close()
    return Result.ok(f"{username} login facebook success")


async def instagram_login(username: str, password: str):
    browser_ = await get_browser()
    instagram_home = "https://www.instagram.com/"
    page: Page = await browser_.new_page()
    await page.set_viewport_size({"width": 1920, "height": 1080})
    await page.evaluate("() => document.body.style.zoom='90%'")
    print(f"GO TO {instagram_home}")
    await page.goto(instagram_home)
    login_button_xpath = '//button[.//div[text()="Log in"]]'
    login_button_locator = page.locator(login_button_xpath)
    try:
        if await login_button_locator.is_visible():
            user_name_input_xpath = '//input[@name="username"]'
            await page.fill(user_name_input_xpath, username)
            sleep(1)

            password_input_xpath = '//input[@name="password"]'
            await page.fill(password_input_xpath, password)
            sleep(1)

            await login_button_locator.click()
            sleep(1)

            save_info_button = '//button[.//div[text()="Save info"]]'
            save_button_locator = page.locator(save_info_button)
            if await save_button_locator.is_visible():
                await save_button_locator.click()
            sleep(1)

        home_span = page.locator("//span[text()='Home' or text()='主页']")
        await home_span.wait_for(state="visible", timeout=5000)  # 等待元素可见
        await page.close()
    except Exception as e:
        return Result.fail_with_msg(f"instagram [{username}] login failed:{e.args[0]}")
    return Result.ok(f"instagram [{username}] login success")


@app.get("/login")
async def login(platform: str, username: str, password: str):
    if platform == "instagram":
        return await instagram_login(username, password)
    if platform == "facebook":
        return await fb_login(username, password)


@app.get("/scrape")
async def scrape(request: Request):
    print("scrape")

    body = await request.body()
    data = json.loads(body)

    link = data.get("link")
    type_ = data.get("type")

    if type_ == "instagram":
        return await instagram_parse(link)
    if type_ == "facebook":
        return await fb_parse(link)

    return {
        "link": link,
        "type": type_,
    }


def parse_args():
    global chrome_cache
    global chrome_exe

    print("parse args")
    parser = argparse.ArgumentParser(
        add_help=True,
        usage="scraper [option] ... [arg] ...",
    )

    try:
        parser.add_argument(
            "--cache",
            type=str,
            help="Cache Path.",
        )

        parser.add_argument(
            "--exe",
            type=str,
            help="exe Path.",
        )
    except Exception as e:
        print(f"Error retrieving environment variables: {e}")
        print(json.dumps(Result.fail_with_msg(f"Error retrieving environment variables:").to_dict()))
        sys.exit(1)

    args = parser.parse_args()
    chrome_cache = args.cache
    chrome_exe = args.exe

    if not chrome_exe:
        print(json.dumps(Result.fail_with_msg(f"cache is empty").to_dict()))
        sys.exit(1)

    if not chrome_cache:
        print(json.dumps(Result.fail_with_msg(f"exe is empty").to_dict()))
        sys.exit(1)


def main():
    uvicorn.run(app, host="0.0.0.0", port=8000)


async def close_page():
    global browser
    if playwright:
        try:
            await playwright.stop()
        except Exception as e:
            logging.error(f"Error stopping Playwright: {e}")
    browser = None


async def create_page():
    print("create playwright browser")
    global playwright, browser
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch_persistent_context(  # 指定本机用户缓存地址
        channel="chrome",
        user_data_dir=chrome_cache,
        # 指定本机google客户端exe的路径
        executable_path=chrome_exe,
        # 要想通过这个下载文件这个必然要开  默认是False
        accept_downloads=True,
        # 设置不是无头模式
        headless=False,
        bypass_csp=True,
        slow_mo=10,
        locale='en-SG',
        # 跳过检测
        args=['--disable-blink-features=AutomationControlled'])
    await browser.grant_permissions(["notifications"], origin="https://www.facebook.com/")
    await browser.grant_permissions(["notifications"], origin="https://www.instagram.com/")
    await browser.grant_permissions(["notifications"], origin="https://x.com/")
    await browser.grant_permissions(["notifications"], origin="https://www.tiktok.com/")
    logging.info("Browser launched successfully.")


if __name__ == '__main__':
    import uvicorn

    multiprocessing.freeze_support()
    parse_args()
    main()
