"""Real Chromium/WebGL2 Office integration; synthetic bounded API, real modules.

Optional test runtime only: pip install playwright; playwright install chromium.
PLAYWRIGHT_BROWSERS_PATH=.map-venv/browsers .map-venv/bin/python -m pytest \
  tests/test_grid_office_browser.py --noconftest -o addopts='' -q
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pw = pytest.importorskip('playwright.sync_api')
WEB = Path(__file__).resolve().parents[1] / 'web_chat'


def snapshot(big=False, empty=False):
    tasks = [
        {'id': 'a', 'title': 'Build the office', 'state': 'in_progress', 'priority': 1, 'created_at': '2026-01-01', 'depends_on': []},
        {'id': 'b', 'title': 'Check the office', 'state': 'blocked', 'priority': 0, 'created_at': '2026-01-02', 'depends_on': ['a']},
        {'id': 'c', 'title': 'Scouting complete', 'state': 'closed', 'created_at': '2026-01-03', 'close_reason': 'Modules and API inspected; acceptance is separate.'},
    ]
    chats = [
        {'chat_id': 'a', 'title': 'Implementation', 'active': True, 'task_links': [{'task_id': 'a', 'relation': 'turn'}]},
        {'chat_id': 'b', 'title': 'Approval', 'active': True, 'awaiting_approval': True, 'task_links': [{'task_id': 'b', 'relation': 'plan'}]},
        {'chat_id': 'c', 'title': 'Idle scout', 'active': False},
    ]
    files = [{'name': 'docs', 'path': 'docs', 'kind': 'dir'}, {'name': 'README.md', 'path': 'README.md', 'kind': 'file'}]
    if big:
        tasks = [{'id': f't{i}', 'title': f'Task {i}', 'state': 'ready', 'priority': i % 5} for i in range(300)]
        chats = [{'chat_id': f'c{i}', 'title': f'Desk {i}', 'active': i % 3 == 0, 'awaiting_approval': i % 7 == 0} for i in range(50)]
        files = [{'name': f'file{i}.md', 'path': f'file{i}.md', 'kind': 'file'} for i in range(100)]
    if empty:
        tasks, chats, files = [], [], []
    return {
        'project': {'id': 'p', 'title': 'GRID · Office verification'},
        'path': [{'id': 'p', 'title': 'GRID'}],
        'tasks': {'available': not empty, 'items': tasks, 'total': 900 if big else len(tasks), 'truncated': big},
        'chats': {'available': True, 'items': chats, 'total': len(chats)},
        'files': {'available': True, 'items': files, 'total': len(files)},
        'files_url': '/files?context=p&path=project',
    }


def detail():
    return {
        'chat': {'id': 'a', 'title': 'Implementation', 'href': '/?conversation=a'},
        'messages': {'items': [{'id': 'm', 'role': 'assistant', 'preview': 'Office is ready for independent review', 'kind': 'final', 'turn_id': 't'}], 'total': 1},
        'executions': {'items': [{'id': 'e', 'turn_id': 't', 'agent': 'Builder', 'status': 'running', 'start_time': 1700000000}], 'total': 1},
        'operations': {'items': [
            {'id': 'read', 'execution_id': 'e', 'kind': 'tool', 'title': 'Read modules', 'status': 'done', 'files': ['docs/a.md']},
            {'id': 'delegate', 'execution_id': 'e', 'kind': 'agent', 'title': 'Reviewer', 'status': 'done'},
            {'id': 'child', 'execution_id': 'e', 'parent_id': 'delegate', 'kind': 'tool', 'title': 'Check render', 'status': 'error'},
        ], 'total': 3},
    }


def shell():
    html = (WEB / 'grid.html').read_text()
    return html.replace('<script type="module" src="/static/js/grid/main.js"></script>', '''<script type="module">
import { GridPage } from '/static/js/grid/page.js';
import { probeWebGL } from '/static/js/grid/view.js';
const ids = {status:'grid-status',title:'grid-title',breadcrumb:'grid-breadcrumb',map:'grid-map',
 space:'grid-space',office:'grid-office',projection:'grid-projection',view:'grid-view',overview:'grid-overview',refresh:'grid-refresh'};
const nodes = Object.fromEntries(Object.entries(ids).map(([k,v]) => [k,document.getElementById(v)]));
window.gridPage = new GridPage(nodes, {projectId:'p',win:window,doc:document,storage:localStorage,
  webgl:probeWebGL(document)});
await window.gridPage.load(); await window.gridPage.viewReady; window.ready = true;
</script>''')


class Served:
    def __init__(self, **kwargs):
        self.map = snapshot(**kwargs)
        self.requested = []
        self.detail_reads = 0

    def route(self, route):
        path = urlsplit(route.request.url).path
        self.requested.append(path)
        if path == '/':
            route.fulfill(body=shell(), content_type='text/html')
        elif path.startswith('/static/'):
            rel = path.removeprefix('/static/')
            target = WEB / rel
            if '..' not in rel and target.is_file():
                route.fulfill(path=str(target), content_type='text/css' if target.suffix == '.css' else 'text/javascript')
            else:
                route.fulfill(status=404)
        elif path == '/api/projects/p/map':
            if route.request.headers.get('if-none-match') == '"map"':
                route.fulfill(status=304, headers={'ETag': '"map"'})
            else:
                route.fulfill(body=json.dumps(self.map), content_type='application/json', headers={'ETag': '"map"'})
        elif path.startswith('/api/projects/p/map/chats/'):
            self.detail_reads += 1
            route.fulfill(body=json.dumps(detail()), content_type='application/json')
        else:
            route.fulfill(status=404, body='not part of this snapshot')


@pytest.fixture
def browser():
    with pw.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(args=['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        except pw.Error as exc:
            pytest.skip(f'Chromium runtime missing: {exc}')
        yield browser
        browser.close()


def open_page(browser, served, **context_options):
    context = browser.new_context(viewport={'width': 1440, 'height': 1100}, **context_options)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/*', served.route)
    page.goto('http://office.test/')
    page.wait_for_function('window.ready === true')
    return context, page, errors


def test_office_real_webgl_selection_idle_lifecycle(browser, tmp_path):
    served = Served()
    context, page, errors = open_page(browser, served)
    assert page.locator('#grid-view').input_value() == 'office'
    assert page.locator('.grid-office-canvas').count() == 1
    assert '/static/js/grid/office.js' in served.requested
    assert '/static/js/grid/space.js' not in served.requested
    assert served.detail_reads == 0
    page.wait_for_function('gridPage.officeView.stats().frames > 0')
    stats = page.evaluate('gridPage.officeView.stats()')
    assert stats['drawCalls'] <= 2
    assert stats['labels'] <= 24
    assert stats['objects'] >= 12
    page.wait_for_timeout(150)
    before = page.evaluate('gridPage.officeView.stats().frames')
    page.wait_for_timeout(200)
    assert page.evaluate('gridPage.officeView.stats().frames') == before
    page.screenshot(path=str(tmp_path / 'office-overview.png'), full_page=True)
    assert page.locator('.grid-office-hint').inner_text().startswith('Select a desk')
    # A real canvas click goes through screenRay -> AABB, not a hidden DOM hit.
    point = page.evaluate('''async () => {
      const {cameraViewProjection, projectPoint} = await import('/static/js/grid/space_math.js');
      const {createOfficeModel} = await import('/static/js/grid/office_model.js');
      const view = gridPage.officeView, canvas = view.canvas, rect = canvas.getBoundingClientRect();
      const model = createOfficeModel(gridPage.viewScene, {map:gridPage.map, selectedId:null});
      const min = model.bounds.min, max = model.bounds.max;
      const radius = Math.hypot(max[0]-min[0], max[1]-min[1], max[2]-min[2]) / 2;
      const desk = model.nodes.find(n => n.id === 'chat:a');
      const screen = projectPoint(cameraViewProjection(view.stats().camera, rect.width/rect.height, radius),
        [desk.position[0], desk.position[1], desk.position[2]], rect.width, rect.height);
      return {x:rect.left+screen.x, y:rect.top+screen.y};
    }''')
    page.mouse.click(point['x'], point['y'])
    page.wait_for_function('gridPage.sceneId() === "chat:a"')
    assert served.detail_reads == 1
    assert 'recorded operations' in page.locator('.grid-office-hint').inner_text()
    page.locator('.grid-office-directory summary').click()
    page.locator('.grid-office-object[data-scene-id="chat:a"]').click()
    page.wait_for_function('gridPage.officeView.stats().objects > 15')
    assert served.detail_reads == 1
    assert page.evaluate('gridPage.sceneId()') == 'chat:a'
    assert page.evaluate('gridPage.chatDetails.has("a")')
    assert page.evaluate('gridPage.scene.nodes.some(n => n.kind === "operation")')
    assert 'recorded operations' in page.locator('.grid-office-hint').inner_text()
    # Native directory selects the exact child operation, with no network replay.
    page.locator('.grid-office-object[data-scene-id="operation:a:child"]').click()
    assert page.evaluate('gridPage.sceneId()') == 'operation:a:child'
    assert served.detail_reads == 1
    page.locator('.grid-office-action', has_text='Focus').click()
    page.wait_for_timeout(450)
    page.locator('.grid-office-canvas').press('Home')
    page.wait_for_timeout(450)
    page.screenshot(path=str(tmp_path / 'office-desktop.png'), full_page=True)
    page.evaluate('gridPage.load()')  # ETag/304 preserves detail and scene.
    assert served.detail_reads == 1
    for _ in range(3):
        page.select_option('#grid-view', 'layers')
        page.wait_for_function('gridPage.officeView === null')
        page.select_option('#grid-view', 'office')
        page.wait_for_function('gridPage.officeView !== null')
        assert page.locator('.grid-office-canvas').count() == 1
    assert page.evaluate('gridPage.sceneId()') == 'operation:a:child'
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.evaluate('gridPage.officeView.canvas.getContext("webgl2").getExtension("WEBGL_lose_context").loseContext()')
    page.wait_for_function('gridPage.view === "layers"')
    assert 'context was lost' in page.locator('#grid-status').inner_text()
    assert not errors
    context.close()


@pytest.mark.parametrize('kind', ['mobile', 'reduced', 'no-webgl'])
def test_office_accessible_defaults_never_import_3d(browser, kind):
    served = Served()
    options = {'reduced_motion': 'reduce'} if kind == 'reduced' else {}
    context = browser.new_context(viewport={'width': 390 if kind == 'mobile' else 1440, 'height': 900}, **options)
    page = context.new_page()
    if kind == 'no-webgl':
        page.add_init_script('HTMLCanvasElement.prototype.getContext = () => null')
    page.route('**/*', served.route)
    page.goto('http://office.test/')
    page.wait_for_function('window.ready === true')
    assert page.locator('#grid-view').input_value() == 'layers'
    assert '/static/js/grid/office.js' not in served.requested
    assert '/static/js/grid/space_math.js' not in served.requested
    assert page.locator('canvas').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if kind == 'no-webgl':
        page.select_option('#grid-view', 'office')
        page.wait_for_function('gridPage.view === "layers"')
        assert 'WebGL2' in page.locator('#grid-status').inner_text()
    context.close()


@pytest.mark.parametrize('kind', ['empty', 'big'])
def test_office_empty_large_and_immediate_reduced_focus(browser, kind):
    served = Served(**{kind: True})
    context, page, errors = open_page(browser, served)
    page.wait_for_function('gridPage.officeView && gridPage.officeView.stats().frames > 0')
    stats = page.evaluate('gridPage.officeView.stats()')
    assert stats['parts'] < 5000
    assert stats['drawCalls'] <= 2
    assert stats['labels'] <= 24
    if kind == 'empty':
        assert 'Tasks unavailable' in page.evaluate('gridPage.officeView ? document.querySelector(".grid-office-root").textContent : ""') or '0 tasks' in page.locator('.grid-office-snapshot').inner_text()
        assert 'No closure reports' in page.locator('.grid-office-relations').inner_text()
    else:
        assert '300/900' in page.locator('.grid-office-snapshot').inner_text()
        assert stats['objects'] == 454
        page.evaluate('gridPage.officeView.update({reducedMotion:true}); gridPage.officeView.focus("chat:c0")')
        camera = page.evaluate('gridPage.officeView.stats().camera')
        assert camera['distance'] < 50
    assert not errors
    context.close()
