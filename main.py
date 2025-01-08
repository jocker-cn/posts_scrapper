import argparse
import asyncio
import contextlib

import json
import logging
import multiprocessing
import re
import sys
from datetime import datetime
from time import sleep

from fastapi import FastAPI, Request
from playwright.async_api import async_playwright, BrowserContext, Page


from result import Result

browser: BrowserContext = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',stream=sys.stdout)

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
    await page.set_viewport_size({"width": 1920, "height": 1080})
    # 设置页面的缩放比例，例如将页面内容缩放至90%
    await page.evaluate("() => document.body.style.zoom='90%'")
    facebook_home = "https://www.facebook.com/login/"
    logging.info(f"GO TO {facebook_home}")
    await page.goto(facebook_home)


    login_button_locator = page.locator('//button[@id="loginbutton"] | //button[@data-testid="royal_login_button"]')
    try:
        try:
            logging.info("wait dialog cookie policy")
            cookie_popup_div = page.locator('//div[contains(@aria-label, "拒绝使用非必要 Cookie")] | //span[text()="Decline optional cookies"]')
            if await cookie_popup_div.count() > 0:
                logging.info("click first cookie policy choose")
                await cookie_popup_div.first.wait_for(state="visible", timeout=3000)  # 等待最多3秒
                await cookie_popup_div.first.click()
        except Exception as e:
            logging.warning("No Cookie policy", e)
        if await login_button_locator.is_visible():
            await page.wait_for_function("window.location.href.startsWith('https://www.facebook.com/login/')",timeout=6000 * 10 * 4)
            logging.info("Login Page Load normal")

            await page.fill('//input[@autocomplete="username"] | //input[@data-testid="royal_email"]', username)
            sleep(1)
            await page.fill('//input[@autocomplete="current-password"] | //input[@data-testid="royal_pass"]', password)
            sleep(1)
            await login_button_locator.click()
            await page.wait_for_load_state('load', timeout=10000)  # 10秒等待加载完成

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

            if await page.wait_for_function("window.location.href === 'https://www.facebook.com/'",
                                            timeout=6000 * 10 * 4):
                await page.goto("https://www.facebook.com/")
                await page.wait_for_function(f"window.location.href === 'https://www.facebook.com/'",
                                             timeout=6000 * 10 * 4)
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
    # if browser:
    #     try:
    #         browser.close()
    #     except asyncio.exceptions.CancelledError:
    #         logging.error("Connection closed while reading from the driver")
    #     except TargetClosedError:
    #         logging.error("Target page, context or browser has been closed")
    #     except Exception as e:
    #         if e.args[0] == "Event loop is closed! Is Playwright already stopped?":
    #             logging.error(f"Error closing browser: {e.args[0]}")
    #         else:
    #             logging.error(f"Error closing browser: {e}")
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
    browser
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
