"""Real Chromium layout regression. Optional: pip install pytest playwright;
PLAYWRIGHT_BROWSERS_PATH=... playwright install chromium, then pytest --noconftest this file.
All routes are synthetic; no network or live project is used.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

playwright = pytest.importorskip('playwright.sync_api')
WEB = Path(__file__).resolve().parents[1] / 'web_chat'


@pytest.mark.parametrize('width', [320, 375, 390, 720, 1280])
@pytest.mark.parametrize('failed', [True, False])
def test_mobile_error_and_nodes_fit_without_scaling(width, failed):
    tasks = {'available': True, 'items': [
        {'id': 'prj-mobile-a', 'title': 'Readable synthetic task', 'state': 'ready'},
        {'id': 'prj-mobile-b', 'title': 'A second synthetic task', 'state': 'blocked'},
    ], 'total': 2}
    if failed:
        tasks = {'available': False, 'reason_code': 'bd_failed',
                 'reason': "The project's local tracker could not be read.",
                 'diagnosis': {'message': 'The tracker database could not be opened. This alone does not establish corruption; check the server diagnostic.', 'exit_code': 1}}
    data = {'project': {'title': 'Synthetic mobile project'}, 'tasks': tasks,
            # A working chat: its card carries the last step and the link count,
            # which the flow layout must show in full, not as a clipped line.
            'processes': {'available': True, 'items': [
                {'chat_id': 'chat-mobile-a', 'title': 'A synthetic working chat',
                 'active': True, 'awaiting_approval': False,
                 'steps': ['read the map answer', 'drew the process cards'],
                 'task_links': [{'task_id': 'prj-mobile-a', 'relation': 'turn'}],
                 'task_links_total': 1},
            ]},
            'files': {'available': True, 'items': []}}
    html = '''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/styles/base.css"><link rel="stylesheet" href="/styles/grid.css">
<div class="gridPage gridShell"><div class="gridBody"><div id="map" class="gridMap__viewport"></div>
<aside id="overview" class="gridOverview"></aside></div></div>
<script type="module">
import {renderMap, renderOverview} from '/js/grid/render.js';
const data = DATA;
renderMap(document.querySelector('#map'), data, {projectId:'prj-mobile'});
renderOverview(document.querySelector('#overview'), data, {projectId:'prj-mobile'});
window.ready=true;
</script>'''.replace('DATA', json.dumps(data))
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': 850})
        def route(request):
            path = urlsplit(request.request.url).path
            if path == '/':
                request.fulfill(body=html, content_type='text/html')
            elif path.startswith(('/styles/', '/js/')) and '..' not in path:
                target = WEB / path.lstrip('/')
                request.fulfill(path=str(target), content_type='text/css' if target.suffix == '.css' else 'text/javascript')
            else:
                request.abort()
        page.route('**/*', route)
        page.goto('http://map.test/')
        page.wait_for_function('window.ready === true')
        if width <= 720:
            metrics = page.evaluate('''() => {
              const region = document.querySelector('[data-region="tasks"]');
              const note = region.querySelector('.gridMap__note');
              const canvas = document.querySelector('.gridMap__canvas');
              const links = document.querySelector('.gridMap__links');
              return {overflow: document.documentElement.scrollWidth > innerWidth,
                transform: getComputedStyle(canvas).transform,
                regionHeight: region.getBoundingClientRect().height,
                noteFits: !note || note.getBoundingClientRect().bottom <= region.getBoundingClientRect().bottom,
                fonts: [...document.querySelectorAll('.gridMap__note--off')].map(n => parseFloat(getComputedStyle(n).fontSize)),
                targets: [...document.querySelectorAll('.gridNode')].map(n => n.getBoundingClientRect().height),
                linksHidden: !links || getComputedStyle(links).display === 'none',
                steps: [...document.querySelectorAll('.gridNode__step')].map(n => ({
                  font: parseFloat(getComputedStyle(n).fontSize),
                  clipped: n.scrollWidth > n.clientWidth + 1}))};
            }''')
            assert not metrics['overflow'], metrics
            assert metrics['transform'] == 'none', metrics
            assert metrics['noteFits'], metrics
            assert all(size >= 14 for size in metrics['fonts']), metrics
            assert all(height >= 44 for height in metrics['targets']), metrics
            assert metrics['regionHeight'] < 500, metrics
            # A line between boxes means nothing once the canvas is a document.
            assert metrics['linksHidden'], metrics
            # The step is readable in the flow, at a real size and in full.
            assert metrics['steps'], metrics
            assert all(step['font'] >= 12 for step in metrics['steps']), metrics
            assert not any(step['clipped'] for step in metrics['steps']), metrics
        else:
            assert page.locator('.gridMap__region').count() == 3
        processes = page.locator('[data-region="processes"]').inner_text()
        assert 'Active: drew the process cards' in processes, processes
        assert '1 task' in processes, processes
        browser.close()
