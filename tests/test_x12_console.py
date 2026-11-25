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


def test_llmwiki_card_copy_matches_new_engine(page):
    """N02：卡片按新引擎（nashsu/llm_wiki）介绍，不再提下载后台程序、
    Apple 芯片限制或"整理时要关闭桌面版 / 只能用命令行"这些旧限制。"""
    card = page.split('id="w-engine-llmwiki"', 1)[1].split(
        '<div class="hidden" id="w2-check"', 1)[0]
    for text in ("用 LLM Wiki 桌面应用整理（开源，nashsu/llm_wiki）",
                 "有图谱、问答",
                 "整理用的模型在 LLM Wiki 里选",
                 "支持 Anthropic、OpenAI 等 API 和 Claude Code、Codex",
                 "需要先安装 LLM Wiki，并保持它在后台运行"):
        assert text in card
    for gone in ("下载", "关闭 LLM Wiki 桌面版", "只能用 Claude Code",
                 "大小待确认", "Apple 芯片"):
        assert gone not in card


def test_step2_guidance_is_actionable_per_reason(page):
    """第 2 步按原因代码给出可照做的指引，并提供「重新检查」按钮。"""
    guidance = function(page, "llmwikiGuidance", "renderW2Check")
    assert "github.com/nashsu/llm_wiki 的 Releases 下载 0.6.13" in guidance
    assert "升级到 0.6.13 或更新版本" in guidance
    assert "位置选 " in guidance and "名称填 " in guidance
    assert "回到第 1 步" in guidance
    # 位置和名称从第 1 步的输入框动态取，不写死。
    assert "$('w-kb-root').value" in guidance
    assert 'id="w2-recheck"' in page
    check = function(page, "checkLlmwikiEngine", "refreshLlmwikiModel")
    assert "post('/api/setup/engine',{engine:'llmwiki'},240000)" in check


def test_step2_blocks_next_until_check_passes(page):
    """检查不通过不能进下一步；模型未配置是唯一例外（放进第 3 步处理）。"""
    next_step = function(page, "wizNext", "saveLlm")
    assert "llmwikiGuidance(reason)" in next_step
    assert next_step.index("reason==='llmwiki_model_missing'") < next_step.index("wizStep=3")
    # 检查通过才保存引擎、进第 3 步。
    assert "llmwikiHealthResult.ok" in next_step


def test_step3_llmwiki_panel_replaces_own_llm_form(page):
    """选 LLM Wiki 时隐藏 xhs-wiki 自己的表单，改为指引 + 检查结果 + 测试。"""
    panel = page.split('id="w-llmwiki-panel"', 1)[1].split(
        'id="w-llmwiki-testnote"', 1)[0]
    for text in ("设置 → LLM Models", "xhs-wiki 不保存你的密钥",
                 "Ingest model", "Anthropic-compat",
                 "https://api.anthropic.com", "claude-haiku-5-5",
                 "Claude 5 系列"):
        assert text in panel
    # 表单切换：面板/表单互斥，保存按钮变成「重新检查」。
    mode = function(page, "syncStep3Mode", "onProviderChange")
    assert "$('w-llm-fields').classList.toggle('hidden',wiki)" in mode
    assert "$('w-llmwiki-panel').classList.toggle('hidden',!wiki)" in mode
    assert "重新检查" in mode


def test_step3_check_result_and_permission_button(page):
    """检查结果显示提供方/模型/密钥（不含密钥内容），权限过宽可一键收紧。"""
    render = function(page, "renderLlmwikiModel", "pickEngine")
    for text in ("模型提供方", "整理用模型（Ingest model）",
                 "不需要（命令行或本机模型）", "本机其他账号也能读",
                 "收紧为仅本人可读", "llmwiki_wrong_project"):
        assert text in render or text in page.split("function renderLlmwikiModel")[1]
    assert "post('/api/setup/llmwiki-tighten',{})" in render
    # 密钥只显示 是/否：渲染代码里没有任何地方读密钥字段。
    assert "apiKey" not in render


def test_step3_test_button_flow(page):
    """测试按钮：先确认费用；项目不匹配时不可用；期间禁用按钮防重复点击。"""
    handler = page.split("$('w-test-llm').addEventListener", 1)[1].split(
        "$('w-qr-refresh')", 1)[0]
    assert "一次很小的模型调用费用" in handler
    assert "llmwikiModelInfo.wrong_project" in handler
    assert "post('/api/setup/test-llmwiki',{confirm_cost:true},330000)" in handler
    assert "一并清理" in handler
    assert "saveBtn.disabled=true;testBtn.disabled=true" in handler


def test_step3_next_rechecks_engine_before_advancing(page):
    """LLM Wiki 引擎的「下一步」重做健康检查，通过才进第 4 步。"""
    next_step = function(page, "wizNext", "saveLlm")
    assert "checkLlmwikiEngine()" in next_step
    assert "引擎检查未通过" in next_step


def test_daily_page_llmwiki_extras(page):
    """日常页：待审核条数（读不到不显示）、权限提示和收紧按钮。"""
    assert 'id="kb-review-span"' in page
    assert "LLM Wiki 待审核" in page
    assert "在 LLM Wiki 的审核页处理，不影响整理" in page
    assert "kb.review_pending==null" in page
    # 同名重复页：有才显示，文件名用 textContent 写入。
    assert 'id="kb-dup-span"' in page
    assert "kb.duplicate_pages" in page
    assert "$('kb-dup-list').textContent=" in page
    assert 'id="e-llmwiki-perm"' in page
    assert "明文保存在设置文件里" in page
    assert "post('/api/setup/llmwiki-tighten',{})" in page
    # LLM Wiki 引擎时隐藏日常页的提供方/模型输入框。
    assert "$('e-llm-fields').classList.toggle('hidden',wikiEngine)" in page


def test_assets_bar_has_no_llmwiki_kind(page):
    """资源下载区不再有「LLM Wiki 后台」一项。"""
    render = function(page, "renderAssets", "el")
    assert "llmwiki" not in render
    assert "LLM Wiki 后台" not in render
    assert "['xhs_downloader','asr_model']" in render


def test_dead_engine_codes_removed_from_labels(page):
    """旧引擎的错误代码不再出现在页面（新引擎的代码另有专项测试）。"""
    for code in ("llmwiki_agent_missing", "llmwiki_wrapper_failed",
                 "llmwiki_provider_unsupported", "llmwiki_desktop_running",
                 "llmwiki_unsupported_platform", "llmwiki_review_required",
                 "llmwiki_binary", "backend_start_timeout", "后台程序"):
        assert code not in page, code



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


# ---------------------------------------------------------------------------
# 执行页面真实 JavaScript（假 DOM / 假 fetch），覆盖 N02 的 LLM Wiki 流程
# ---------------------------------------------------------------------------

N02_SCENARIOS = {
    # 第 2 步：选中 → 健康检查不通过（没装应用）→ 指引可见；
    # 重新检查通过 → 检查结果区显示通过 → 下一步进第 3 步并自动取检查结果。
    "wizard_llmwiki_full_flow": r"""
    await run("engineChoice='llmwiki';engineTouched=true;wizStep=2");
    responses.push({status:400,data:{error:'engine_unavailable',reason:'llmwiki_app_missing'}});
    await run("pickEngine('llmwiki')");
    assert(element('w2-guidance').textContent.includes('github.com/nashsu/llm_wiki'));
    assert.equal(await run('wizStep'),2);
    // 下一步被不通过的检查挡住（再次发起检查，仍失败）。
    responses.push({status:400,data:{error:'engine_unavailable',reason:'llmwiki_app_missing'}});
    await run('wizNext()');
    assert.equal(await run('wizStep'),2);
    assert(element('wiz-hint').textContent.includes('没找到 LLM Wiki'));
    // 重新检查通过 → 检查结果区显示通过 → 下一步进第 3 步（自动取模型信息）。
    responses.push({status:200,data:{ok:true}});
    responses.push({status:200,data:{ok:true,model:{provider:'自定义提供方',model:'claude-haiku-5-5',has_key:true,key_needed:true},wrong_project:false,permission:null}});
    await element('w2-recheck').events.click();
    await run('wizNext()');
    assert.equal(await run('wizStep'),3);
    assert.equal(element('w-save-llm').textContent,'重新检查');
    assert(requests.some(r=>r.path==='/api/setup/llmwiki-model'));
    // 第 3 步的下一步：重做健康检查 + 刷新模型信息，通过后进第 4 步。
    responses.push({status:200,data:{ok:true}});
    responses.push({status:200,data:{ok:true,model:{provider:'自定义提供方',model:'claude-haiku-5-5',has_key:true,key_needed:true},wrong_project:false,permission:null}});
    await run('wizNext()');
    assert.equal(await run('wizStep'),4);
    assert(element('w3-hint').textContent.includes('检查通过'));
    """,
    # 模型未配置不算第 2 步的拦路问题：直接放进第 3 步处理。
    "wizard_model_missing_enters_step3": r"""
    await run("engineChoice='llmwiki';engineTouched=true;wizStep=2");
    responses.push({status:400,data:{error:'engine_unavailable',reason:'llmwiki_model_missing'}});
    responses.push({status:200,data:{ok:true,model:{provider:null,model:null,has_key:false,key_needed:true},wrong_project:false,permission:null}});
    await run('wizNext()');
    assert.equal(await run('wizStep'),3);
    assert(element('wiz-hint').textContent.includes('整理用的模型还没配好'));
    """,
    # 日常页引擎表单选 LLM Wiki：只保存引擎选择，不改 xhs-wiki 的模型设置。
    "daily_engine_llmwiki_only_posts_engine": r"""
    await run("last={settings:{engine:'markdown'}};$('e-engine').value='llmwiki'");
    responses.push({status:400,data:{error:'engine_unavailable',reason:'llmwiki_app_missing'}});
    await element('engine-form').events.submit({preventDefault(){}});
    assert.deepEqual(requests.map(r=>r.path),['/api/setup/engine']);
    assert(requests[0].body.engine==='llmwiki');
    assert(element('engine-hint').textContent.includes('没找到 LLM Wiki'));
    assert(!element('engine-hint').textContent.includes('已保存'));
    """,
    # 从 LLM Wiki 切回轻量并选 API 提供方：先切引擎再保存提供方（顺序不变）。
    "markdown_switch_before_api_provider": r"""
    await run("last={settings:{engine:'llmwiki'}};$('e-engine').value='markdown';$('e-provider').value='anthropic';$('e-model').value='generated-model'");
    responses.push({status:200,data:{}},{status:200,data:{}});
    await element('engine-form').events.submit({preventDefault(){}});
    assert.deepEqual(requests.map(r=>r.path),['/api/setup/engine','/api/setup/llm']);
    assert(requests[0].body.engine==='markdown');
    assert(element('engine-hint').textContent.startsWith('已保存'));
    """,
}


@pytest.mark.parametrize("scenario", sorted(N02_SCENARIOS))
def test_n02_wizard_and_daily_javascript(tmp_path, scenario):
    """执行页面实际事件，覆盖 N02 的 LLM Wiki 向导与日常表单分支。

    假 fetch 逐项消费预设响应（GET 也一样），任何多余请求都会使测试失败。
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("可选的本地 JavaScript 运行器 Node 未安装；不安装新依赖")
    script = tmp_path / "n02_console_events.js"
    script.write_text(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
// 注册全部实际事件，但跳过页面末尾的首次刷新和定时轮询。
const source=html.split('<script>')[1].split('</script>')[0].split('\nonProviderChange();\nshowWizStep();\nrefresh();')[0];
const nodes=new Map(),responses=[],requests=[];
function element(id){
 if(nodes.has(id))return nodes.get(id);
 const n={id,value:'',textContent:'',disabled:false,style:{},dataset:{},events:{},attrs:{},options:[],children:[],classList:{add(){},remove(){},toggle(){}},setAttribute(k,v){this.attrs[k]=v},replaceChildren(...items){this.children=items},append(...items){this.children.push(...items)},querySelector(){return null},addEventListener(k,f){this.events[k]=f}};
 if(id==='w-provider'){n.options=['claude_code','codex','anthropic','openai_compatible'].map(value=>({value}));n.value='claude_code'}
 nodes.set(id,n);return n;
}
const context=vm.createContext({document:{getElementById:element,querySelector:()=>({content:'fake'}),createElement:()=>element('node-'+Math.random()),createTextNode:t=>({textContent:t})},AbortSignal:{timeout:n=>n},Intl,Date,console,confirm:()=>true,alert:m=>{throw Error(m)},
 fetch:async(path,options)=>{options=options||{};requests.push({path,body:options.body?JSON.parse(options.body):null});assert(responses.length,'出现未安排的请求：'+path);const r=responses.shift();return {status:r.status,json:async()=>r.data,ok:r.status>=200&&r.status<300}}});
vm.runInContext(source,context);
let refreshed=0;context.recordRefresh=()=>{refreshed++};vm.runInContext('refresh=recordRefresh',context);
async function run(code){return vm.runInContext(code,context)}
(async()=>{
""" + N02_SCENARIOS[scenario] + r"""
assert.equal(responses.length,0);console.log('控制台事件验证通过');
})().catch(e=>{console.error(e);process.exitCode=1});
""", encoding="utf-8")
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
