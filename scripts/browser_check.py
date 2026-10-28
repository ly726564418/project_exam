"""Browser integration checks using a locally installed Edge or Chromium."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

BASE=os.environ.get('ATLAS_TEST_URL','http://127.0.0.1:8000')
OUT=Path('docs/screenshots');OUT.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1600,'height':1100},device_scale_factor=1)
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(BASE)
    expect(page.locator('#metric-objects')).not_to_have_text('0')
    demos=page.locator('#run-select option').evaluate_all("options => options.map(o => o.value).filter(v => v.startsWith('demo-'))")
    if demos:
        page.locator('#run-select').select_option(demos[0])
    page.wait_for_timeout(1500)
    assert page.locator('#universe').is_visible()
    page.screenshot(path=str(OUT/'explore-desktop.png'),full_page=True)
    page.locator('.recent-item').first.click()
    page.wait_for_selector('.detail-title')
    assert page.locator('.attribute-row').count()>2
    page.locator('#toggle-raw').click()
    assert page.locator('#object-json').is_visible()
    page.keyboard.press('Escape')
    assert not page.locator('#detail-drawer').is_visible()
    page.locator('[data-view="catalog"]').click()
    page.wait_for_selector('#object-rows tr[data-object]')
    page.locator('#object-search').fill('不存在的对象-xyz')
    page.locator('#search-form button[type="submit"]').click()
    expect(page.locator('#object-rows')).to_contain_text('暂无匹配')
    page.locator('#object-search').fill('')
    page.locator('#search-form button[type="submit"]').click()
    page.wait_for_selector('#object-rows tr[data-object]')
    page.locator('#next-page').click()
    expect(page.locator('#page-note')).to_contain_text('第 2 页')
    page.locator('#previous-page').click()
    expect(page.locator('#page-note')).to_contain_text('第 1 页')
    page.locator('[data-view="schema"]').click()
    page.wait_for_selector('#field-rows .kind-tag')
    page.screenshot(path=str(OUT/'schema-desktop.png'),full_page=True)
    page.locator('[data-view="quality"]').click()
    page.wait_for_selector('.quality-stat')
    assert page.locator('.quality-stat').count()>=6
    page.locator('#open-control').click()
    assert page.locator('#control-dialog').is_visible()
    page.keyboard.press('Escape')
    page.locator('[data-view="explore"]').click()
    page.locator('#zoom-in').click()
    page.locator('#zoom-fit').click()
    page.locator('#live-toggle').click()
    assert page.locator('#live-toggle').get_attribute('aria-pressed')=='false'
    page.locator('#live-toggle').click()
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_timeout(700)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'mobile horizontal overflow'
    page.screenshot(path=str(OUT/'explore-mobile.png'),full_page=True)
    assert not errors,errors
    print(json.dumps({'browser':'Microsoft Edge headless','desktop':'1600x1100','mobile':'390x844','console_errors':errors,'checks':12,'screenshots':str(OUT)},ensure_ascii=False))
    browser.close()
