# -*- coding: utf-8 -*-
"""test_x12_console.py —— X12 向导与发布说明的离线回归检查。

本文件从实际交付的 HTML 读取卡片、资源状态和提供方限制，按项目既有
页面测试方式检查关键分支，不启动浏览器、HTTP 服务或任何后台程序。
说明文档同时检查，防止页面可选而安装文档仍引导手改路径。
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


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
    """两种语言都说明来源、双重校验和审阅边界，同时如实保留发布前占位限制。"""
    for name in ("README.md", "README_EN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for expected in ("tree/headless", "GitHub Actions", "SHA-256",
                         "install-llmwiki", "v2.0.6", "Codex", "Apache-2.0"):
            assert expected in text
    chinese = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "控制台本身不提供审阅按钮" in chinese
    assert "发布前占位" in chinese
    english = (ROOT / "README_EN.md").read_text(encoding="utf-8")
    assert "console has no review buttons" in english
    assert "release placeholders" in english


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


def test_revision_documents_both_optional_claude_flags():
    """维护说明明确能力检测、打印模式与避免重复，不能回退到无条件追加。"""
    architecture = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    llmwiki = architecture.split("### 6.2 ", 1)[1].split("## 7.", 1)[0]
    for value in ("--safe-mode", "--no-session-persistence", "--print", "原参数没有"):
        assert value in llmwiki
