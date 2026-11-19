# -*- coding: utf-8 -*-
"""test_x12_console.py —— X12 向导与发布说明的离线回归检查。

本文件从实际交付的 HTML 读取卡片、资源状态和提供方限制，按项目既有
页面测试方式检查关键分支，不启动浏览器、HTTP 服务或任何后台程序。
说明文档同时检查，防止页面可选而安装文档仍引导手改路径。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from xhs_wiki.store import SKILL_TARGETS


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def page():
    """每项检查读取实际发布页面，避免在测试副本里修复而漏改交付物。"""
    return (ROOT / "src/xhs_wiki/web/console.html").read_text(encoding="utf-8")


def function(page, name, next_name):
    """限定到目标函数的代码段，让同名文字出现在别处不能误使测试通过。"""
    return page.split("function " + name + "(", 1)[1].split(
        "function " + next_name + "(", 1)[0]


def test_llmwiki_card_explains_download_and_constraints(page):
    """卡片交代桌面浏览、额外下载和整理前关闭桌面版，不再要求另装桌面版。"""
    card = page.split('id="w-engine-llmwiki"', 1)[1].split(
        '<p class="hint" id="w2-hint">', 1)[0]
    for text in ("图谱和版本管理", "第一次选择会额外下载", "关闭 LLM Wiki 桌面版",
                 "只能用 Claude Code 或 Codex", "点一下开始下载"):
        assert text in card
    assert "即将提供" not in card
    assert "需要另外安装桌面版" not in card


def test_llmwiki_size_comes_from_status_bytes(page):
    """下载大小来自状态字节数，未知时不编造 0 MB 或固定下载量。"""
    render = function(page, "renderLlmwikiCard", "checkLlmwikiEngine")
    assert "Number(a.total_bytes)" in render
    assert "Math.ceil(size/1000000)" in render
    assert "大小待确认" in render
    assert "(data.assets||{}).llmwiki" in page


def test_unsupported_card_is_disabled_at_click_entry(page):
    """既提供无障碍禁用状态，也在实际点击入口拒绝选择，避免仅变灰。"""
    render = function(page, "renderLlmwikiCard", "checkLlmwikiEngine")
    pick = function(page, "pickEngine", "wizNext")
    assert "card.setAttribute('aria-disabled',String(unsupported))" in render
    assert "card.tabIndex=unsupported?-1:0" in render
    assert "name==='llmwiki'&&llmwikiSupported===false)return" in pick


def test_download_progress_and_retry_stay_on_card(page):
    """下载有百分比/未知大小两种显示，失败原因翻译后保留再次点击入口。"""
    render = function(page, "renderLlmwikiCard", "checkLlmwikiEngine")
    assert "a.state==='failed'" in render
    assert "plainCode(a.error||'assets_error')" in render
    assert "点一下重试" in render
    assert "a.progress==null?'下载中…'" in render
    assert "Math.round(Math.max(0,Math.min(1,a.progress))*100)" in render


def test_ready_resource_checks_health_once_after_selection(page):
    """状态轮询只在用户选择的资源就绪后发起健康检查，成功失败都记结果。"""
    render = function(page, "renderLlmwikiCard", "checkLlmwikiEngine")
    check = function(page, "checkLlmwikiEngine", "pickEngine")
    assert "if(llmwikiChecking)" in render
    assert "if(llmwikiHealthResult)" in render
    assert "a.state==='ready'" in render
    assert "engineChoice==='llmwiki'&&(llmwikiRequested||!engineTouched)" in render
    assert "checkLlmwikiEngine()" in render
    assert "ok:r.status===200" in check
    assert "if(r.status===202)" in check
    assert "llmwikiAsset=r.data.progress" in check


def test_next_remains_clickable_and_does_not_advance_during_download(page):
    """下载中的下一步给等待提示并提前返回，不把 202 当成功。"""
    next_step = function(page, "wizNext", "saveLlm")
    assert "['downloading','pending'].includes(llmwikiAsset.state)" in next_step
    assert "wizHint('后台程序还在下载');return" in next_step
    assert "if(r.status===202){wizHint('后台程序还在下载');return}" in next_step
    assert next_step.index("if(r.status===202)") < next_step.index("wizStep=3")
    assert "$('w-next').disabled" not in page


def test_llmwiki_removes_api_options_and_resets_invalid_choice(page):
    """过滤实际下拉节点；切回轻量恢复完整列表，旧 API 选择转 Claude Code。"""
    filtering = function(page, "filterWizardProviders", "renderLlmwikiCard")
    assert "engineChoice==='llmwiki'" in filtering
    assert "!limited||['claude_code','codex'].includes(o.value)" in filtering
    assert "select.replaceChildren(...options)" in filtering
    assert "select.value=allowed?previous:'claude_code'" in filtering
    assert "modelTouched=false;providerTouched=true" in filtering
    assert 'id="w-provider-engine-tip"' in page


def test_optional_asset_does_not_block_all_ready_banner(page):
    """LLM Wiki 的 not_needed 要在 allReady=false 之前跳过。"""
    render = function(page, "renderAssets", "el")
    assert "['xhs_downloader','asr_model','llmwiki']" in render
    assert render.index("a.state==='not_needed'") < render.index("allReady=false")


def test_switching_to_markdown_releases_wizard_request(page):
    """明确把轻量选择交给服务端，使后台取消 LLM Wiki 的必需资源状态。"""
    pick = function(page, "pickEngine", "wizNext")
    assert "llmwikiRequested=false" in pick
    assert "post('/api/setup/engine',{engine:'markdown'})" in pick


def test_llmwiki_and_download_errors_have_chinese_labels(page):
    """锁定新增代码和复用的下载失败代码，避免角标退回内部英文错误。"""
    labels = dict(re.findall(r"^\s*([a-z0-9_]+):'([^']*)'", page, re.M))
    for code in ("llmwiki_unsupported_platform", "llmwiki_agent_missing",
                 "llmwiki_provider_unsupported", "llmwiki_desktop_running",
                 "llmwiki_wrapper_failed", "assets_sha256_mismatch",
                 "assets_network_error", "assets_size_mismatch",
                 "assets_extract_failed", "assets_error"):
        assert re.search(r"[\u4e00-\u9fff]", labels[code]), code


def test_readmes_describe_release_integrity_and_pending_review():
    """两种语言都说明来源、双重校验和审阅边界，并指向清单里实际下载的那个 Release。

    发布前的"清单还是占位值"说法不能留在文档里；Release 标签从清单读取，
    以后换新版后台时，忘了同步 README 会在这里报错。
    """
    manifest = json.loads(
        (ROOT / "src/xhs_wiki/assets_manifest.json").read_text(encoding="utf-8"))
    release_tag = "releases/tag/headless-v" + manifest["llmwiki"]["version"]
    for name in ("README.md", "README_EN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for expected in ("tree/headless", "GitHub Actions", "SHA-256",
                         "install-llmwiki", "v2.0.6", "Codex", "Apache-2.0",
                         release_tag):
            assert expected in text, (name, expected)
    chinese = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "控制台本身不提供审阅按钮" in chinese
    assert "发布前占位" not in chinese and "发布占位" not in chinese
    english = (ROOT / "README_EN.md").read_text(encoding="utf-8")
    assert "console has no review buttons" in english
    assert "release placeholder" not in english


def test_docs_say_llmwiki_pages_are_always_simplified_chinese():
    """v0.2.1 起页面固定用简体中文：文档不能再说"跟随桌面版界面语言"。

    README 两种语言和技术设计都要写明固定中文和对应的环境变量，避免文档
    和 backend.py 实际传给后台的值对不上。
    """
    chinese = (ROOT / "README.md").read_text(encoding="utf-8")
    english = (ROOT / "README_EN.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    assert "整理出的页面固定用简体中文写" in chinese
    assert "页面语言跟随" not in chinese
    assert "always written in Simplified Chinese" in english
    assert "follow the interface language" not in english
    assert "页面语言跟随桌面版的界面语言" not in design
    for text in (chinese, english, design):
        assert "LLM_WIKI_CONTENT_LANGUAGE=zh-CN" in text


def test_missing_default_agent_can_choose_provider_before_health_passes(page):
    """缺少默认代理仅放行选择提供方，后续保存必须等引擎健康通过。"""
    next_step = function(page, "wizNext", "saveLlm")
    save = page.split("async function saveLlm(){", 1)[1].split(
        "/* ============ 事件绑定", 1)[0]
    assert "r.data.reason==='llmwiki_agent_missing'" in next_step
    assert "请先选择本机已安装的 Claude Code 或 Codex" in next_step
    assert "engineResult=await checkLlmwikiEngine()" in save
    assert "engineResult.status!==200" in save
    assert save.index("engineResult.status!==200") < save.index("hint.textContent='已保存 ✓'")
    assert "post('/api/setup/finish',{},240000)" in next_step


def test_wizard_javascript_with_fake_dom_and_fetch(tmp_path):
    """有 Node 的开发环境执行真实页面逻辑；没有则仍保留上面的静态门槛。

    Node 只运行本地字符串和假的 DOM、fetch，不启动浏览器、不绑定端口，
    也不允许未安排的请求。脚本在 pytest 临时目录中，避免留下调试文件。
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("可选的本地 JavaScript 运行器 Node 未安装；不安装新依赖")
    script = tmp_path / "wizard_fake_dom.js"
    script.write_text(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
const script=html.split('<script>')[1].split('</script>')[0].split('/* ============ 事件绑定 ============ */')[0];
const nodes=new Map();
// 只实现被向导实际用到的 DOM 方法，其余动作都留给断言检查。
function element(id){
 if(nodes.has(id))return nodes.get(id);
 const n={id,value:'',textContent:'',style:{},dataset:{},attrs:{},options:[],children:[],classList:{add(){},remove(){},toggle(){}},setAttribute(k,v){this.attrs[k]=v},replaceChildren(...items){this.children=items;if(id==='w-provider')this.options=items},append(){},querySelector(){return element(id+'-child')}};
 if(id==='w-provider'){n.options=['claude_code','codex','anthropic','openai_compatible'].map(value=>({value}));n.value='claude_code'}
 nodes.set(id,n);return n;
}
const responses=[],requests=[];
const context=vm.createContext({document:{getElementById:element,querySelector:()=>({content:'fake'}),createElement:()=>element('generated'),createTextNode:()=>({})},AbortSignal:{timeout:()=>null},Intl,Date,console,fetch:async(path,options)=>{requests.push({path,body:JSON.parse(options.body)});assert(responses.length,'unexpected request');const r=responses.shift();return {status:r.status,json:async()=>r.data}}});
vm.runInContext(script,context);
async function run(code){return vm.runInContext(code,context)}
(async()=>{
 await run("llmwikiSupported=true;llmwikiAsset={state:'not_needed',total_bytes:12500000};renderLlmwikiCard()");
 assert.equal(element('w-engine-llmwiki-size').textContent,'约 13 MB 的后台程序');
 responses.push({status:202,data:{progress:{state:'downloading',progress:.42}}});
 await run("pickEngine('llmwiki')");
 assert.equal(element('w-engine-llmwiki-badge').textContent,'下载中 42%');
 await run('wizStep=2;wizNext()');
 assert.equal(element('wiz-hint').textContent,'后台程序还在下载');assert.equal(requests.length,1);
 responses.push({status:200,data:{}});
 await run("llmwikiAsset={state:'ready'};renderLlmwikiCard()");await new Promise(setImmediate);
 assert.equal(element('w-engine-llmwiki-badge').textContent,'可用');assert.equal(requests.length,2);
 await run('renderLlmwikiCard();renderLlmwikiCard()');assert.equal(requests.length,2);
 await run("llmwikiAsset={state:'failed',error:'assets_network_error'};renderLlmwikiCard()");
 assert(element('w-engine-llmwiki-badge').textContent.includes('点一下重试'));
 responses.push({status:202,data:{progress:{state:'downloading',progress:null}}});
 await run("pickEngine('llmwiki')");assert.equal(element('w-engine-llmwiki-badge').textContent,'下载中…');
 // 缺少默认 Claude 时只进入提供方选择，不误认为已保存引擎。
 await run("llmwikiAsset={state:'ready'}");
 responses.push({status:400,data:{reason:'llmwiki_agent_missing'}});
 await run('wizStep=2;wizNext()');assert.equal(await run('wizStep'),3);
 await run("$('w-provider').value='codex';onProviderChange()");
 responses.push({status:200,data:{}},{status:400,data:{reason:'llmwiki_agent_missing'}});
 await run('wizNext()');assert.equal(await run('wizStep'),3);
 responses.push({status:200,data:{}},{status:200,data:{}});
 await run('wizNext()');assert.equal(await run('wizStep'),4);
 assert(requests.some(r=>r.path==='/api/setup/llm'&&r.body.provider==='codex'));
 // 其他健康错误不放行；正在检查时不能发出并发切换请求。
 responses.push({status:400,data:{reason:'llmwiki_desktop_running'}});
 await run('wizStep=2;wizNext()');assert.equal(await run('wizStep'),2);
 const beforeBusy=requests.length;
 await run("llmwikiChecking=true;pickEngine('markdown')");
 assert.equal(requests.length,beforeBusy);assert.equal(await run('engineChoice'),'llmwiki');
 await run("llmwikiChecking=false;engineChoice='markdown';filterWizardProviders();$('w-provider').value='anthropic';engineChoice='llmwiki';onProviderChange()");
 assert.equal(element('w-provider').value,'claude_code');assert.equal(element('w-provider').options.length,2);
 await run("engineChoice='markdown';filterWizardProviders()");assert.equal(element('w-provider').options.length,4);
 await run('llmwikiSupported=false;renderLlmwikiCard()');const count=requests.length;
 await run("pickEngine('llmwiki')");assert.equal(requests.length,count);assert.equal(element('w-engine-llmwiki').attrs['aria-disabled'],'true');
 assert.equal(responses.length,0);
 console.log('向导真实 JavaScript 与假 DOM/fetch 验证通过');
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding="utf-8")
    result = subprocess.run(
        [node, str(script), str(ROOT / "src/xhs_wiki/web/console.html")],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "验证通过" in result.stdout


@pytest.mark.parametrize("scenario", [
    "restart_restores_saved_engine", "markdown_before_api",
    "markdown_failure_keeps_provider", "reject_llmwiki_api",
    "daily_download_is_pending", "daily_health_reason_is_translated",
])
def test_reenter_wizard_and_daily_engine_changes(tmp_path, scenario):
    """执行页面实际事件，覆盖重新设置与日常表单受新引擎规则影响的分支。

    假 fetch 逐项消费预设响应，任何多余请求都会使测试失败；刷新只记录
    次数，不访问本地服务。这里既核对最终提示，也核对请求顺序和请求体。
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("可选的本地 JavaScript 运行器 Node 未安装；不安装新依赖")
    scenarios = {
        "restart_restores_saved_engine": r'''
await run("engineTouched=true;providerTouched=true;modelTouched=true;limitTouched=true;kbRootLoaded=true;engineChoice='llmwiki';wizStep=5;llmwikiHealthResult={ok:true};filterWizardProviders()");
responses.push({status:200,data:{}});
await element('restart-setup').events.click();
assert.equal(await run('wizStep'),1);assert.equal(await run('engineTouched'),false);
assert.equal(await run('llmwikiHealthResult'),null);assert.equal(element('w-provider').options.length,4);
assert.equal(element('w-kb-root').disabled,true);
await run("renderWizard({llmwiki_supported:true,settings:{engine:'markdown',llm:{provider:'anthropic'},kb_root:'/tmp/generated-wiki'},assets:{llmwiki:{state:'not_needed'}}})");
assert.equal(await run('engineChoice'),'markdown');assert.equal(element('w-provider').value,'anthropic');
assert.equal(await run('kbRootLoaded'),true);assert.equal(requests.length,1);
''',
        "markdown_before_api": r'''
await run("last={settings:{engine:'llmwiki'}};$('e-engine').value='markdown';$('e-provider').value='openai_compatible';$('e-model').value='generated-model';$('e-model').dataset.base_url='https://api.example.test/v1'");
responses.push({status:200,data:{}},{status:200,data:{}});
await element('engine-form').events.submit({preventDefault(){}});
assert.deepEqual(requests.map(r=>r.path),['/api/setup/engine','/api/setup/llm']);
assert.equal(requests[0].body.engine,'markdown');assert.equal(requests[1].body.provider,'openai_compatible');
assert.equal(requests[1].body.base_url,'https://api.example.test/v1');
assert(element('engine-hint').textContent.startsWith('已保存'));
''',
        "markdown_failure_keeps_provider": r'''
await run("last={settings:{engine:'llmwiki'}};$('e-engine').value='markdown';$('e-provider').value='anthropic';$('e-model').value='generated-model'");
responses.push({status:400,data:{reason:'engine_unavailable'}});
await element('engine-form').events.submit({preventDefault(){}});
assert.equal(requests.length,1);assert.equal(requests[0].path,'/api/setup/engine');
assert.equal(element('engine-hint').textContent,'引擎保存失败：这个引擎暂不可用');
''',
        "reject_llmwiki_api": r'''
await run("last={settings:{engine:'markdown'}};$('e-engine').value='llmwiki';$('e-provider').value='anthropic';$('e-model').value='generated-model'");
await element('engine-form').events.submit({preventDefault(){}});
assert.equal(requests.length,0);
assert.equal(element('engine-hint').textContent,'LLM Wiki 引擎只能用 Claude Code 或 Codex 整理');
''',
        "daily_download_is_pending": r'''
await run("last={settings:{engine:'markdown'}};$('e-engine').value='llmwiki';$('e-provider').value='codex';$('e-model').value=''");
responses.push({status:200,data:{}},{status:202,data:{state:'downloading',progress:{state:'downloading'}}});
await element('engine-form').events.submit({preventDefault(){}});
assert.deepEqual(requests.map(r=>r.path),['/api/setup/llm','/api/setup/engine']);
assert.equal(requests[0].body.model,'default');assert.equal(requests[1].timeout,240000);
assert(element('engine-hint').textContent.includes('后台程序还在下载'));
assert(element('engine-hint').textContent.includes('重新设置'));
assert(!element('engine-hint').textContent.includes('已保存'));
''',
        "daily_health_reason_is_translated": r'''
await run("last={settings:{engine:'markdown'}};$('e-engine').value='llmwiki';$('e-provider').value='claude_code';$('e-model').value='generated-model'");
responses.push({status:200,data:{}},{status:400,data:{reason:'llmwiki_desktop_running'}});
await element('engine-form').events.submit({preventDefault(){}});
assert.equal(element('engine-hint').textContent,'引擎保存失败：LLM Wiki 桌面版正在运行，请先关闭再试');
''',
    }
    script = tmp_path / "console_events.js"
    script.write_text(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
// 注册全部实际事件，但跳过页面末尾的首次刷新和定时轮询。
const source=html.split('<script>')[1].split('</script>')[0].split('\nonProviderChange();\nshowWizStep();\nrefresh();')[0];
const nodes=new Map(),responses=[],requests=[];
function element(id){
 if(nodes.has(id))return nodes.get(id);
 const n={id,value:'',textContent:'',disabled:false,style:{},dataset:{},events:{},attrs:{},options:[],children:[],classList:{add(){},remove(){},toggle(){}},setAttribute(k,v){this.attrs[k]=v},replaceChildren(...items){this.children=items;if(id==='w-provider')this.options=items},append(){},querySelector(){return element(id+'-child')},addEventListener(k,f){this.events[k]=f}};
 if(id==='w-provider'){n.options=['claude_code','codex','anthropic','openai_compatible'].map(value=>({value}));n.value='claude_code'}
 nodes.set(id,n);return n;
}
let refreshed=0;
const context=vm.createContext({document:{getElementById:element,querySelector:()=>({content:'fake'}),createElement:()=>element('generated'),createTextNode:()=>({})},AbortSignal:{timeout:n=>n},Intl,Date,console,confirm:()=>true,alert:message=>{throw Error(message)},fetch:async(path,options)=>{requests.push({path,body:JSON.parse(options.body),timeout:options.signal});assert(responses.length,'出现未安排的请求');const r=responses.shift();return {status:r.status,json:async()=>r.data}}});
vm.runInContext(source,context);
context.recordRefresh=()=>{refreshed++};
vm.runInContext('refresh=recordRefresh',context);
async function run(code){return vm.runInContext(code,context)}
(async()=>{
''' + scenarios[scenario] + r'''
assert.equal(responses.length,0);console.log('控制台事件验证通过');
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding="utf-8")
    result = subprocess.run(
        [node, str(script), str(ROOT / "src/xhs_wiki/web/console.html")],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "验证通过" in result.stdout

# ---------------------------------------------------------------------------
# "在哪些 AI 工具里提问"（向导第 6 步和日常页共用的勾选框）
# ---------------------------------------------------------------------------

def test_skill_target_text_covers_every_target(page):
    """页面上每个安装位置都有给人看的文字，键和 store.SKILL_TARGETS 一一对应；
    只有 Claude Code 和 Codex 命令行实测过，其余都如实写"还没实测"。"""
    block = page.split("const SKILL_TARGET_TEXT={", 1)[1].split("\n};", 1)[0]
    keys = re.findall(r"^\s*(\w+):\{title:", block, re.M)
    assert keys == list(SKILL_TARGETS)
    checks = dict(re.findall(r"^\s*(\w+):\{title:'[^']*',check:'([^']*)'", block, re.M))
    assert set(checks) == set(SKILL_TARGETS)
    assert checks["claude_code"] == "实测过"
    assert checks["agents"].startswith("Codex 命令行实测过")
    for target in SKILL_TARGETS:
        if target not in ("claude_code", "agents"):
            assert checks[target] == "还没实测", target
    # Gemini CLI 已换成 Antigravity CLI、Qoder CN 分 IDE 和命令行，都要写清。
    assert "Antigravity CLI" in block and "2026-06-18" in block
    assert "Qoder CN 命令行版" in block and "VS Code 里的 GitHub Copilot" in block


SKILL_SCENARIOS = {
    "rows_highlight_and_draft": r"""
await run("updateSkillTargetRows('daily',"+JSON.stringify(targets({workbuddy:{detected_tools:['WorkBuddy']}}))+")");
const box=byId('d-skill-targets');
assert.deepEqual(box.querySelectorAll('input[type=checkbox]').map(i=>i.dataset.target),IDS);
const more=box.querySelector('[data-role=more]');
assert.equal(more.open,false);
assert.equal(more.querySelectorAll('input[type=checkbox]').length,IDS.length-3);
const wb=box.querySelector('input[data-target="workbuddy"]');
assert(text(wb.closest('.choice')).includes('本机有 WorkBuddy'));
assert(text(wb.closest('.choice')).includes('（还没实测）'));
assert(text(box.querySelector('input[data-target="claude_code"]').closest('.choice')).includes('（实测过）'));
assert(!wb.closest('.choice').classList.contains('on'));
// 点一下立刻高亮，不用等下一次轮询。
wb.checked=true;wb.events.change();
assert(wb.closest('.choice').classList.contains('on'));
assert.equal(JSON.stringify(await run('skillDraft.daily')),JSON.stringify(['claude_code','workbuddy']));
// 轮询拿到的还是旧设置：用户没保存的勾选不被覆盖。
await run("updateSkillTargetRows('daily',"+JSON.stringify(targets())+")");
assert.equal(wb.checked,true);
assert(wb.closest('.choice').classList.contains('on'));
// 一切正常的保存：提示"已保存"，草稿清掉，之后跟着服务端走。
responses.push({status:200,data:{skill_targets:targets({workbuddy:{selected:true,installed:true,state:'installed',last_result:'installed'}}),results:{claude_code:'installed',workbuddy:'installed'}}});
await byId('d-skill-save').events.click();
assert.equal(JSON.stringify(requests[0].body.targets),JSON.stringify(['claude_code','workbuddy']));
assert(byId('d-skill-hint').textContent.startsWith('已保存 ✓'));
assert.equal(await run('skillDraft.daily'),null);
assert(text(wb.closest('.choice')).includes('已装好'));
""",
    "save_reports_each_problem": r"""
await run("updateSkillTargetRows('daily',"+JSON.stringify(targets())+")");
const box=byId('d-skill-targets');
const pick=id=>box.querySelector('input[data-target="'+id+'"]');
for(const id of ['workbuddy','agents']){pick(id).checked=true;pick(id).events.change()}
responses.push({status:200,data:{
  skill_targets:targets({workbuddy:{selected:true,last_result:'failed'},
                         agents:{selected:true,state:'foreign',last_result:'kept_foreign'},
                         kiro:{installed:true,state:'installed',last_result:'failed'},
                         cline:{symlink:true,last_result:'kept_symlink'}}),
  results:{claude_code:'installed',workbuddy:'failed',agents:'kept_foreign',kiro:'failed',cline:'kept_symlink'}}});
await byId('d-skill-save').events.click();
assert.equal(JSON.stringify(requests[0].body.targets),JSON.stringify(['claude_code','workbuddy','agents']));
const hint=byId('d-skill-hint');
assert(hint.className.includes('err'));
assert(hint.textContent.includes('这些工具的技能没写成功：WorkBuddy（腾讯）'));
assert(hint.textContent.includes('这些工具的技能没撤掉：Kiro（AWS）'));
assert(hint.textContent.includes('这些位置已有别人写的同名技能，没有覆盖也没有删：Codex'));
assert(hint.textContent.includes('这些位置的技能文件夹是符号链接，没有动它：Cline'));
assert(!hint.textContent.includes('已保存'));
assert(text(pick('workbuddy').closest('.choice')).includes('上次没写成功'));
assert(text(pick('kiro').closest('.choice')).includes('上次没撤掉'));
assert(text(pick('agents').closest('.choice')).includes('已有别人写的同名技能，没有动它'));
assert(!text(pick('agents').closest('.choice')).includes('已装好'));
assert(text(pick('cline').closest('.choice')).includes('是符号链接，没有动它'));
""",
    "wizard_save_failure_blocks_finish": r"""
await run("updateSkillTargetRows('wizard',"+JSON.stringify(targets())+")");
await run('wizStep=6');
responses.push({status:400,data:{error:'skill_targets_invalid'}});
await run('wizNext()');
assert.equal(JSON.stringify(requests.map(r=>r.path)),JSON.stringify(['/api/skill-targets']));
assert(byId('w6-hint').textContent.startsWith('保存失败'));
assert.equal(await run('wizStep'),6);
assert.equal(refreshed,0);
""",
    "wizard_partial_failure_still_finishes": r"""
await run("updateSkillTargetRows('wizard',"+JSON.stringify(targets())+")");
const wb=byId('w-skill-targets').querySelector('input[data-target="workbuddy"]');
wb.checked=true;wb.events.change();
await run('wizStep=6');
responses.push(
  {status:200,data:{skill_targets:targets({workbuddy:{selected:true,last_result:'failed'}}),results:{claude_code:'installed',workbuddy:'failed'}}},
  {status:200,data:{ok:true,setup_completed:true,assets_ready:true,skill_results:{claude_code:'installed',workbuddy:'failed'}}});
await run('wizNext()');
assert.equal(JSON.stringify(requests.map(r=>r.path)),JSON.stringify(['/api/skill-targets','/api/setup/finish']));
const hint=byId('wiz-hint').textContent;
assert(hint.startsWith('设置完成，已开始第一次整理 ✓'),hint);
assert.equal(hint.split('WorkBuddy（腾讯）').length-1,1,hint);
assert.equal(refreshed,1);
""",
    "kb_root_change_reports_skill_failure": r"""
await run("$('w-kb-root').value='/tmp/generated-kb';wizStep=1");
responses.push({status:200,data:{ok:true,kb_root:'/tmp/generated-kb',migrated:false,moved:null,skill_results:{claude_code:'installed',agents:'failed'}}});
await run('wizNext()');
assert.equal(await run('wizStep'),2);
const hint=byId('wiz-hint');
assert(hint.textContent.startsWith('这些工具的技能没写成功：Codex'),hint.textContent);
assert(hint.className.includes('err'));
""",
    "daily_engine_change_reports_skill_failure": r"""
await run("last={settings:{engine:'markdown'}};$('e-engine').value='markdown';$('e-provider').value='claude_code';$('e-model').value='generated-model'");
responses.push({status:200,data:{}},{status:200,data:{ok:true,engine:'markdown',skill_results:{claude_code:'installed',kiro:'failed'}}});
await byId('engine-form').events.submit({preventDefault(){}});
const hint=byId('engine-hint');
assert(hint.textContent.startsWith('已保存。新设置只影响之后整理的内容。这些工具的技能没写成功：Kiro（AWS）'),hint.textContent);
assert(hint.className.includes('err'));
""",
}

# 上面这些场景共用的假 DOM 和假 fetch（Node 脚本的开头和结尾）。
SKILL_HARNESS_HEAD = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
const IDS=JSON.parse(process.argv[3]);
// 注册全部实际事件，但跳过页面末尾的首次刷新和定时轮询。
const source=html.split('<script>')[1].split('</script>')[0].split('\nonProviderChange();\nshowWizStep();\nrefresh();')[0];
function matches(n,sel){
 if(!n.tagName)return false;
 let m;
 if((m=sel.match(/^\.([\w-]+)$/)))return n.classList.contains(m[1]);
 if(sel==='input[type=checkbox]')return n.tagName==='input'&&n.type==='checkbox';
 if((m=sel.match(/^input\[data-target="([\w-]+)"\]$/)))return n.tagName==='input'&&n.dataset.target===m[1];
 if((m=sel.match(/^\[data-role=([\w-]+)\]$/)))return n.dataset.role===m[1];
 return false;
}
function makeNode(tag){
 const classes=new Set();
 return {tagName:tag,children:[],parent:null,dataset:{},style:{},attrs:{},events:{},textContent:'',value:'',checked:false,type:'',open:false,disabled:false,options:[],
  get className(){return [...classes].join(' ')},
  set className(v){classes.clear();String(v).split(/\s+/).filter(Boolean).forEach(c=>classes.add(c))},
  classList:{add:(...c)=>c.forEach(x=>classes.add(x)),remove:(...c)=>c.forEach(x=>classes.delete(x)),
    toggle:(c,force)=>{const on=force===undefined?!classes.has(c):Boolean(force);if(on)classes.add(c);else classes.delete(c);return on},
    contains:c=>classes.has(c)},
  get childElementCount(){return this.children.filter(c=>c.tagName).length},
  append(...items){for(const item of items){item.parent=this;this.children.push(item)}},
  replaceChildren(...items){this.children=[];this.append(...items)},
  addEventListener(k,f){this.events[k]=f},
  setAttribute(k,v){this.attrs[k]=v},
  closest(sel){let cur=this;while(cur){if(matches(cur,sel))return cur;cur=cur.parent}return null},
  querySelectorAll(sel){const out=[];const walk=x=>{for(const c of x.children){if(matches(c,sel))out.push(c);if(c.children)walk(c)}};walk(this);return out},
  querySelector(sel){return this.querySelectorAll(sel)[0]||null}};
}
const nodes=new Map(),responses=[],requests=[];
function byId(id){if(!nodes.has(id)){const n=makeNode('div');n.id=id;nodes.set(id,n)}return nodes.get(id)}
// 一个元素连同里面所有文字（标题、标签、提示）拼起来，方便查"页面上写了什么"。
const text=n=>(n.textContent||'')+(n.children||[]).map(text).join('');
function targets(over={}){
 return IDS.map(id=>Object.assign({id,selected:id==='claude_code',detected_tools:[],path:'~/generated/'+id+'/xhs-wiki',
   state:id==='claude_code'?'installed':'absent',installed:id==='claude_code',symlink:false,last_result:null},over[id]||{}));
}
let refreshed=0;
const context=vm.createContext({document:{getElementById:byId,querySelector:()=>({content:'fake'}),createElement:makeNode,createTextNode:t=>({textContent:t})},
 AbortSignal:{timeout:n=>n},Intl,Date,console,confirm:()=>true,alert:message=>{throw Error(message)},
 fetch:async(path,options)=>{requests.push({path,body:JSON.parse(options.body)});assert(responses.length,'出现未安排的请求：'+path);const r=responses.shift();return {status:r.status,json:async()=>r.data}}});
vm.runInContext(source,context);
context.recordRefresh=()=>{refreshed++};
vm.runInContext('refresh=recordRefresh',context);
async function run(code){return vm.runInContext(code,context)}
(async()=>{
"""
SKILL_HARNESS_TAIL = r"""
assert.equal(responses.length,0);console.log('勾选框逻辑验证通过');
})().catch(e=>{console.error(e);process.exitCode=1});
"""


@pytest.mark.parametrize("scenario", sorted(SKILL_SCENARIOS))
def test_skill_targets_javascript_with_fake_dom(tmp_path, scenario):
    """执行页面里真实的勾选框、保存和向导逻辑（假 DOM、假 fetch）。

    这里的假 DOM 比上面的多实现一点：子节点、按类名和属性找元素、classList
    真的记状态，才能检查一行行建出来的样子和高亮。假 fetch 逐项消费预设
    响应，多发一个请求就失败；刷新只记次数，不访问本地服务。
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("可选的本地 JavaScript 运行器 Node 未安装；不安装新依赖")
    script = tmp_path / "skill_targets_fake_dom.js"
    script.write_text(SKILL_HARNESS_HEAD + SKILL_SCENARIOS[scenario] + SKILL_HARNESS_TAIL,
                      encoding="utf-8")
    result = subprocess.run(
        [node, str(script), str(ROOT / "src/xhs_wiki/web/console.html"),
         json.dumps(list(SKILL_TARGETS))],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "验证通过" in result.stdout


def test_revision_documents_both_optional_claude_flags():
    """维护说明明确能力检测、打印模式与避免重复，不能回退到无条件追加。"""
    architecture = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    llmwiki = architecture.split("### 6.2 ", 1)[1].split("## 7.", 1)[0]
    for value in ("--safe-mode", "--no-session-persistence", "--print", "原参数没有"):
        assert value in llmwiki
