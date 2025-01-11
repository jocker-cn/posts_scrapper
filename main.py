# -*- coding: utf-8 -*-

import argparse
import contextlib

import json
import logging
import multiprocessing
import re
import sys
import threading
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


def adjust_tiktok_date(push_time):
    # 获取当前年份
    current_year = datetime.now().year

    # 如果日期格式是 'YYYY-MM-DD'（例如 '2024-05-01'）
    if re.match(r"\d{4}-\d{2}-\d{2}", push_time):
        # 直接将其转为 'YYYY-MM-DD 00:00:00'
        return datetime.strptime(push_time, "%Y-%m-%d").strftime("%Y-%m-%d 00:00:00")

    # 如果日期格式是 'M-D' 或 'MM-DD'（例如 '1-2' 或 '11-25'）
    elif re.match(r"\d{1,2}-\d{1,2}", push_time):
        # 使用当前年份并拼接 'YYYY-MM-DD' 格式
        return datetime.strptime(f"{current_year}-{push_time}", "%Y-%m-%d").strftime("%Y-%m-%d 00:00:00")

    # 如果不符合上述格式，返回原始输入
    return push_time


async def x_parse(link):
    if not browser:
        await create_page()
    page = None
    try:
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1920, "height": 1080})
        await page.goto(link)
        await page.wait_for_selector('//div[@data-testid="User-Name"]', timeout=10000)
        user_info_div = page.locator('//div[@data-testid="User-Name"]')
        user_info_div = user_info_div.locator('//a[@href]')
        profile_id = ""
        if user_info_div and await user_info_div.count() > 0:
            print("user_info_div")
            profile_id = await user_info_div.nth(0).get_attribute("href")
            if profile_id:
                profile_id = profile_id.replace('/', '@')
        username = await user_info_div.locator('span span').text_content()
        profile_url = f"https://x.com/{profile_id.replace('@', '')}"
        post_id = page.url.split('/')[-1]

        hashtags = page.locator('a[href*="/hashtag/"]')
        hashtags_set = set()
        if await hashtags.count() > 0:
            for tag in await hashtags.all():
                tag = await tag.get_attribute("href")
                if tag:
                    hashtags_set.add(f"#{tag.split('/hashtag/')[1].split('?')[0]}")
        tags = list(hashtags_set)

        tweet_text_div = page.locator('//div[@data-testid="tweetText"]')
        tweet_text_div = tweet_text_div.locator('span').nth(0)
        push_content = ""
        if await tweet_text_div.count() > 0:
            tweet_text_div = tweet_text_div.nth(0)
            push_content = await tweet_text_div.text_content()

        push_time = page.locator('//time[@datetime]')
        push_time = await push_time.nth(0).get_attribute("datetime")
        if push_time:
            push_time = datetime.fromisoformat(push_time)
            push_time = push_time.strftime("%Y-%m-%d %H:%M:%S")
        avatar_url = f"{profile_url}/photo"
        share = 0
        likes = 0
        loves = 0
        comments = 0
        views = 0
        count_element = page.locator(
            "//div[@role='group' and (contains(@aria-label, 'replies') or contains(@aria-label, 'reposts') or contains(@aria-label, 'likes') or contains(@aria-label, 'bookmarks') or contains(@aria-label, 'views') or contains(@aria-label, '回复') or contains(@aria-label, '次转贴') or contains(@aria-label, '喜欢') or contains(@aria-label, '书签') or contains(@aria-label, '次观看'))]")
        if await count_element.count() > 0:
            count_element = count_element.nth(0)
            count_element = await count_element.get_attribute('aria-label')
            if count_element != '':
                count_element = count_element.split(',')
                for item in count_element:
                    match = re.search(r'(\d+)', item)
                    if match:
                        value = match.group(1)
                        if '回复' in item or 'replies' in item:
                            comments = value  # 获取回复的数值
                        elif '转帖' in item or 'reposts' in item:
                            share = value  # 获取转帖的数值
                        elif '喜欢' in item or 'likes' in item:
                            likes = value  # 获取喜欢的数值
                        elif '书签' in item or 'bookmarks' in item:
                            loves = value  # 获取书签的数值
                        elif '观看' in item or 'views' in item:
                            views = value  # 获取观看的数值



        return Result.ok({
            "username": username,
            "profileId": profile_id,
            "profileUrl": profile_url,
            "postLink": link,
            "postId": post_id,
            "tags": tags,
            "profileImage": avatar_url,
            "pushTime": push_time,
            "content": push_content,
            "retweets": share,
            "likes": likes,
            "lovers": loves,
            "comments": comments,
            "views": views,
        }).to_dict()
    except Exception as e:
        print(f"post parse exception:{e}")
        await close_page()
        return Result.fail_with_msg(f"x [{link}] parse failed: {e.args[0]}")
    finally:
        if page:
            await page.close()


async def tiktok_parse(link):
    if not browser:
        await create_page()
    page = None
    try:
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1920, "height": 1080})
        await page.goto(link)

        username = await page.wait_for_selector('xpath=//span[@data-e2e="browse-username"]', timeout=10000)
        if username:
            username = await username.text_content()
        profile_url = f"https://www.tiktok.com/@{username}"
        push_time = await page.query_selector('xpath=//span[@data-e2e="browser-nickname"]/span[3]')
        if push_time:
            push_time = await push_time.text_content()
        push_time = adjust_tiktok_date(push_time)

        match = re.search(r"/video/(\d+)", page.url)
        post_id = ""
        if match:
            post_id = match.group(1)

        tag_links = await page.query_selector_all('xpath=//a[starts-with(@href, "/tag/")]')
        tags = []
        for tag in tag_links:
            href = await tag.get_attribute('href')
            if '/tag/' in href:
                tag = '#' + href.split('/tag/')[1]
                tags.append(tag)
        avatar_url = await page.query_selector('xpath=//span[@shape="circle"]//img[@loading="lazy"]')
        if avatar_url:
            avatar_url = await avatar_url.get_attribute('src')
        else:
            avatar_url = ""
        push_content = await page.query_selector('xpath=//h1[@data-e2e="browse-video-desc"]/span[1]')
        if push_content:
            push_content = await push_content.text_content()
        else:
            push_content = ""

        likes = await page.query_selector('xpath=//strong[@data-e2e="like-count"]')
        if likes:
            likes = await likes.text_content()
        likes = parse_number(likes)

        comments = await page.query_selector('xpath=//strong[@data-e2e="comment-count"]')
        if comments:
            comments = await comments.text_content()
        comments = parse_number(comments)

        loves = await page.query_selector('xpath=//strong[@data-e2e="share-count"]')
        if loves:
            loves = await loves.text_content()
        loves = parse_number(loves)

        share = await page.query_selector('xpath=//strong[@data-e2e="undefined-count"]')
        if share:
            share = await share.text_content()
        share = parse_number(share)

        return Result.ok({
            "username": username,
            "profileId": username,
            "profileUrl": profile_url,
            "postLink": link,
            "postId": post_id,
            "tags": tags,
            "profileImage": avatar_url,
            "pushTime": push_time,
            "content": push_content,
            "retweets": share,
            "likes": likes,
            "lovers": loves,
            "comments": comments,
        }).to_dict()
    except Exception as e:
        print(f"post parse exception:{e}")
        await close_page()
        return Result.fail_with_msg(f"tiktok [{link}] parse failed: {e.args[0]}")
    finally:
        if page:
            await page.close()


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
            like_count = 0
            comments = 0
            share = 0
            div_elements = await page.query_selector_all(
                'xpath=//div[@class="x9f619 x1n2onr6 x1ja2u2z x78zum5 xdt5ytf x2lah0s x193iq5w x1xmf6yo x1e56ztr xzboxd6 x14l7nz5"][position() >= 3 and position() <= 5]')
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

        like_count = parse_number(like_count)
        comments = parse_number(comments)
        share = parse_number(share)
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
        await close_page()
        return Result.fail_with_msg(f"fb [{link}] parse failed: {e.args[0]}")
    finally:
        if page:
            await page.close()


def parse_number(number_text):

    if isinstance(number_text, (int, float)):
        return number_text

    number_text = re.sub(r"(likes?|次赞)", "", number_text.strip(), flags=re.IGNORECASE)
    if not number_text or not number_text.strip():
      return 0

    number_text = number_text.replace(",", "").replace(" ", "").strip()

    match = re.match(r"(\d+(\.\d+)?)\s?万", number_text)
    if match:
        return int(float(match.group(1)) * 10000)

    match_k = re.match(r"(\d+(\.\d+)?)\s?K", number_text, re.IGNORECASE)
    if match_k:
        return int(float(match_k.group(1)) * 1000)

    match_m = re.match(r"(\d+(\.\d+)?)\s?M", number_text, re.IGNORECASE)
    if match_m:
        return int(float(match_m.group(1)) * 1000000)

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

        try:
            await page.wait_for_selector('svg[aria-label="Close"]', timeout=2000)
            close_button = page.locator('svg[aria-label="Close"]')
            await close_button.click()
        except:
            pass

        push_datetime = await page.locator("(//time)[last()]").get_attribute("datetime")
        push_time = datetime.strptime(push_datetime, "%Y-%m-%dT%H:%M:%S.%fZ").strftime("%Y-%m-%d %H:%M:%S")
        # 点赞数量
        likes = page.locator("(//a[span[contains(text(), 'likes') or contains(text(), 'like') or contains(text(), '次赞')]])")
        if await likes.count() > 0:
            likes = await likes.text_content()
        else:
            likes = page.locator("(//a[span[contains(text(), 'likes') or contains(text(), 'like') or contains(text(), '次赞')]]/span/span)")
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
            tag_name = href.split("/explore/tags/")[1].strip("/") if href else None
            tags.add(f"#{tag_name}")
        tags = list(tags)
        await page.close()
        likes = parse_number(likes)
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
        })
    except Exception as e:
        await close_page()
        return Result.fail_with_msg(f"instagram parse failed:{e.args[0]}")
    finally:
        if page:
            await page.close()


async def x_login(username: str, password: str):
    TWITTER_LOGIN_URL = "https://x.com/i/flow/login"


async def fb_login(username: str, password: str):
    logging.info("fb login username [%s]", username)
    browser_ = await get_browser()
    try:
        page = await browser_.new_page()
    except Exception as e:
        await close_page()
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
    instagram_home = "https://www.instagram.com/login"
    page: Page = await browser_.new_page()
    try:
        await page.set_viewport_size({"width": 1920, "height": 1080})
        await page.evaluate("() => document.body.style.zoom='90%'")
        print(f"GO TO {instagram_home}")
        await page.goto(instagram_home)
        login_button_xpath = '//button[.//div[text()="Log in"]]'
        await page.wait_for_selector(login_button_xpath, timeout=5000)
        login_button_locator = page.locator(login_button_xpath)
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
        try:
            await page.wait_for_selector('input[type="text"][value=""][name="email"]', timeout=5000)
            input_code = page.locator('input[type="text"][value=""][name="email"]')
            if await input_code.count() > 0:
                captcha_code = await get_input_with_timeout("input instagram code: ",60)
                await input_code.fill(captcha_code)
                continue_button = page.locator('//span[text()="Continue"]')
                await continue_button.click()
        except:
            logging.warning("no instagram code check")

        home_span = page.locator("//span[text()='Home' or text()='主页']")
        await home_span.wait_for(state="visible", timeout=5000)  # 等待元素可见
        await page.close()
    except Exception as e:
        await close_page()
        return Result.fail_with_msg(f"instagram [{username}] login failed:{e.args[0]}")
    finally:
        if page:
            await page.close()
    return Result.ok(f"instagram [{username}] login success")


async def get_input_with_timeout(prompt, timeout):
    result = []
    def input_thread():
        user_input = input(prompt)
        result.append(user_input)

    thread = threading.Thread(target=input_thread)
    thread.daemon = True
    thread.start()

    thread.join(timeout)
    if result:
        return result[0]
    else:
        raise TimeoutError(f"User did not input within {timeout} seconds.")

@app.get("/login")
async def login(platform: str, username: str, password: str):
    if platform == "instagram":
        return await instagram_login(username, password)
    if platform == "facebook":
        return await fb_login(username, password)
    if platform == "x":
        return await  x_login(username, password)


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
    if type_ == "tiktok":
        return await tiktok_parse(link)
    if type_ == "x":
        return await x_parse(link)

    return Result.fail_with_msg(f"not support platform:{type_}")


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
