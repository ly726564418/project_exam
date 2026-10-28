"""Start the official generator through the actual web form, then verify arrival."""
import json
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

base=os.environ.get('ATLAS_TEST_URL','http://127.0.0.1:8000')
with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1400,'height':1000})
    page.goto(base)
    page.locator('#open-control').click()
    run='browser-control-'+str(int(time.time()))
    page.locator('#gen-test-id').fill(run)
    page.locator('#gen-rate').fill('200')
    page.locator('#gen-duration').fill('8')
    page.locator('#gen-batch').fill('20')
    page.locator('#gen-target').fill('http://host.docker.internal:8000/ingest')
    page.locator('#start-generator').click()
    expect(page.locator('#control-dialog')).not_to_be_visible(timeout=15000)
    for _ in range(100):
        result=page.request.get(base+'/api/generator/status')
        assert result.ok,result.text()
        if result.json().get('state')=='finished':break
        time.sleep(.2)
    else:raise AssertionError('generator did not finish')
    receipt=page.request.get(base+'/api/generator/receipt').json()
    for _ in range(100):
        stats=page.request.get(base+'/api/stats').json()
        record=next((r for r in stats['runs'] if r['id']==run),None)
        if record and record['objects']>0 and not stats['statuses'].get('pending',0):break
        time.sleep(.2)
    else:raise AssertionError('no objects arrived through generator control form')
    assert receipt['config']['test_id']==run
    summary={'test_id':run,'form_start_success':True,'generator_proxy_success':True,'received_objects':record['objects'],
        'normal_acknowledged_objects':receipt['stats']['acknowledged_objects'],'pending':stats['statuses'].get('pending',0),'passed':True}
    out=Path('docs/results');out.mkdir(parents=True,exist_ok=True)
    (out/'browser-control.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'browser-control-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    browser.close()
