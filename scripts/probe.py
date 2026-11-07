# -*- coding: utf-8 -*-
"""
生成一个带自检探针的页面副本，用于无头浏览器端到端验证。

产物放在 web/_probe.html —— 它和 index.html 同级，所以经 server 打开时
`fetch('api/payload')` 仍能正确命中后端；验证完记得删掉。

用法:
    python scripts/probe.py            # 生成 web/_probe.html
    python scripts/probe.py --offline  # 给 dist 里的单文件版插探针

探针会依次点过 6 个面板，检查横向溢出，最后把结果写进 <pre id="__dbg">。
"""
import argparse
import glob
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

EARLY = r"""<script>/*早期错误捕获：主脚本抛错时探针还没注册，先把错误存下来*/
window.__EARLY=[];
window.addEventListener("error",function(e){
  window.__EARLY.push("JSERR "+e.message+" @"+(e.lineno||0));
});
window.addEventListener("unhandledrejection",function(e){
  window.__EARLY.push("REJECT "+((e.reason&&e.reason.message)||e.reason));
});
</script>"""

PROBE = r"""
<script>
/* ===== 自检探针（自动生成，勿并入正式产物）===== */
(function(){
  var LOG = [];
  var __pre = null;
  function add(k, v){ LOG.push(k + '=' + v); flush(); }
  function flush(){
    /* 增量落盘：哪步断了，截断的 dump 里也能看到 */
    if (!__pre){
      __pre = document.createElement('pre');
      __pre.id = '__dbg';
      (document.body || document.documentElement).appendChild(__pre);
    }
    __pre.textContent = LOG.join('\n');
  }
  function wait(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  // 先把主脚本执行期间的错误捞过来
  (window.__EARLY || []).forEach(function(l){ LOG.push(l); });
  window.addEventListener('error', function(e){ LOG.push('JSERR ' + e.message); });
  window.addEventListener('unhandledrejection', function(e){
    LOG.push('REJECT ' + (e.reason && e.reason.message || e.reason));
  });

  var VW = document.documentElement.clientWidth;
  function overflow(sel){
    var root = document.querySelector(sel) || document.body;
    var bad = [];
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length; i++){
      var r = all[i].getBoundingClientRect();
      if (r.width > 0 && r.right > VW + 1.5){
        bad.push((all[i].className || all[i].tagName) + '@' + Math.round(r.right));
      }
    }
    return bad.length ? (bad.length + ' [' + bad.slice(0, 3).join(' | ') + ']') : '0';
  }
  function txt(id){
    var e = document.getElementById(id);
    return e ? e.textContent : 'NONE';
  }
  /* 元素不在（面板空态、功能未启用）时返回占位值，别让一个 null 把整条链打断 */
  function attrOf(sel, name){
    var e = document.querySelector(sel);
    return e ? e.getAttribute(name) : 'NO_EL';
  }
  function clickPanel(p){
    var n = document.querySelector('.nav[data-p="' + p + '"]');
    if (n) n.click();
    return wait(900);
  }

  wait(2200).then(function(){
    add('viewport', VW + 'x' + document.documentElement.clientHeight);
    add('bootErrShown', !document.getElementById('bootErr').hidden);
    add('--- 顶栏(来自 meta) ---', '');
    add('brandDate', txt('brandDate'));
    add('tsCap', txt('tsCap'));
    add('tsUsed', txt('tsUsed'));
    add('tsFree', txt('tsFree'));
    add('tsFreeNote', txt('tsFreeNote'));
    add('ovHead', txt('ovHead'));
    add('lgTot', txt('lgTot'));
    add('railPct', txt('railPct'));
    add('railFree', txt('railFree'));
    return clickPanel('overview');
  }).then(function(){
    add('--- 概览 ---', '');
    add('overflow.overview', overflow('#p-overview'));
    add('echartsLoaded', typeof window.echarts);
    add('charts.beforeRowClick', document.querySelectorAll('canvas').length);
    // 环形图与顶栏：不能是上一台机器留下的死数字
    add('ovPct', txt('ovPct'));
    add('ovArc', (function(){
      var a = document.getElementById('ovArc');
      return a ? a.getAttribute('stroke-dasharray') : 'NONE';
    })());
    add('tsCap', txt('tsCap'));
    add('lgUsed', txt('lgUsed'));
    add('lgRec', txt('lgRec'));
    // 报表语义层：指标块 / 结论卡
    add('rpHintHidden', (document.getElementById('rpHint') || {}).hidden);
    add('ovTiles', document.querySelectorAll('#ovTiles .tile').length);
    add('ovTiles.first', (function(){
      var e = document.querySelector('#ovTiles .tile');
      return e ? e.textContent.slice(0, 60) : 'NONE';
    })());
    add('concl', document.querySelectorAll('#concl .cc').length);
    add('concl.metric', (document.querySelector('#concl .cm') || {}).textContent || 'NONE');
    add('concl.factsLeft', (function(){
      // 文字里若还残留 {xxx} 说明事实占位符没替换成功
      var t = (document.getElementById('concl') || {}).textContent || '';
      var m = t.match(/\{[a-z0-9._]+\}/gi);
      return m ? m.slice(0, 4).join(',') : '0';
    })());
    return clickPanel('tree');
  }).then(function(){
    add('--- 目录结构(来自 tree.json) ---', '');
    add('treeRows', document.querySelectorAll('.trow').length);
    add('treeFirstRow', (function(){
      var r = document.querySelector('.trow .tname');
      return r ? r.textContent.slice(0, 40) : 'NONE';
    })());
    add('overflow.tree', overflow('#p-tree'));
    // 点中第一行 → 侧栏「目录用途」卡应展开并画层级图
    var row = document.querySelector('.trow');
    if (row) row.click();
    return wait(1600);
  }).then(function(){
    add('pucardOpen', !!document.querySelector('#purposeCard.open'));
    add('puSpark', document.querySelectorAll('#puBody .tspark').length);
    add('puActBtns', document.querySelectorAll('#puBody .cpb button').length);
    add('driveSelOpts', document.querySelectorAll('#driveSel .dd').length);
    add('charts.afterRowClick', document.querySelectorAll('canvas').length);
    add('chartHost', (function(){
      var c = document.getElementById('puChart');
      if (!c) return 'NONE';
      var r = c.getBoundingClientRect();
      return Math.round(r.width) + 'x' + Math.round(r.height);
    })());
    add('chRead', (document.getElementById('chRead') || {}).textContent || 'NONE');
    add('copyBtns', document.querySelectorAll('.cpb button, .cpb > *').length);
    return clickPanel('dirs');
  }).then(function(){
    add('--- 大目录用途 ---', '');
    add('overflow.dirs', overflow('#p-dirs'));
    add('top5Rows', document.querySelectorAll('#top5 .r').length);   // 末端目录清单（由 tree.json 现算）
    add('top5Bars', document.querySelectorAll('#top5 .tb > i').length);
    add('mgridBars', document.querySelectorAll('#p-dirs .mgrid .m u').length);  // 构成条（JS 按行内最大值补）
    add('tfootSum', (function(){
      var t = document.querySelector('#p-dirs tfoot td.sum');
      return t ? t.textContent : 'NONE';
    })());
    // 报表语义层：主目录表 / 明细卡
    add('dirsBody', document.querySelectorAll('#dirsBody tr').length);
    add('dirsCnt', txt('dirsCnt'));
    add('dirsSum', txt('dirsSum'));
    add('dirsNote', (txt('dirsNote') || '').slice(0, 70));
    add('dirDetails', document.querySelectorAll('#dirDetails .card').length);
    add('dirDetails.rows', document.querySelectorAll('#dirDetails .minitbl tr').length);
    add('top5Head', txt('top5Head'));
    // 图例已挪进使用说明浮层，目录树侧栏不该再有
    add('legendInPop', document.querySelectorAll('#infoPop .legendbox span').length);
    add('drawersInSide', document.querySelectorAll('#p-tree .side .drawer').length);
    return clickPanel('clean');
  }).then(function(){
    add('--- 清理指引 ---', '');
    add('overflow.clean', overflow('#p-clean'));
    // 报表语义层：A / B / C 三块
    add('cleanA', document.querySelectorAll('#cleanA .cleanrow').length);
    add('cleanA.ck', document.querySelectorAll('#cleanA .ck').length);
    add('totalA', txt('totalA'));
    add('selCnt', txt('selCnt'));
    add('selTot', txt('selTot'));
    add('allA', txt('allA'));
    add('cleanBCardHidden', (document.getElementById('cleanBCard') || {}).hidden);
    add('cleanBSum', txt('cleanBSum'));
    add('cleanB.items', document.querySelectorAll('#cleanB .bi').length);
    add('cleanB.mk', document.querySelectorAll('#cleanB .mk').length);
    add('cleanC', document.querySelectorAll('#cleanC tr').length);
    // 禁区表 / 陷阱 / 执行顺序已并入本页
    add('forbidBody', document.querySelectorAll('#forbidBody tr').length);
    add('forbidCnt', txt('forbidCnt'));
    add('trapsCardHidden', (document.getElementById('trapsCard') || {}).hidden);
    add('traps', document.querySelectorAll('#traps .trap').length);
    add('stepsCardHidden', (document.getElementById('stepsCard') || {}).hidden);
    add('steps', document.querySelectorAll('#steps .step').length);
    add('stot', (txt('stot') || '').slice(0, 40));
    return clickPanel('moved');
  }).then(function(){
    add('--- 已迁移目录(来自 links.json) ---', '');
    var tb = document.querySelector('#p-moved tbody');
    add('movedRows', tb ? tb.children.length : -1);
    add('navMovedCnt', txt('navMovedCnt'));
    add('mvOff', txt('mvOff'));
    add('mvPct', txt('mvPct'));
    add('mvCnt', txt('mvCnt'));
    add('mvCap', txt('mvCap'));
    add('mvAggBar', (document.getElementById('mvAggBar') || {}).style ?
        document.getElementById('mvAggBar').style.width : 'NONE');
    add('overflow.moved', overflow('#p-moved'));
    add('migList', document.querySelectorAll('#migList .bl').length);
    add('migNote', (txt('migNote') || '').replace(/\s+/g, ' ').slice(0, 70));
    // 静态文案里的 %TGT%（实体在哪块盘）与口径说明里的文件/目录计数
    add('movedHead', (function(){
      var p = document.querySelector('#p-moved .phead p');
      return p ? p.textContent.replace(/\s+/g, ' ').slice(0, 60) : 'NONE';
    })());
    add('fileCnt', (document.querySelector('.fileCnt') || {}).textContent || 'NONE');
    add('dirCnt', (document.querySelector('.dirCnt') || {}).textContent || 'NONE');
    add('minMbSubTxt', txt('minMbSubTxt'));
    add('tokenLeft', (function(){
      // 页面上不该再残留 %HOME% / %USER% / %DRIVE% / %SYSDRIVE% / %TGT%
      var t = document.body.innerText || '';
      var m = t.match(/%(HOME|USER|DRIVE|SYSDRIVE|TGT)%/g);
      return m ? m.slice(0, 4).join(',') : '0';
    })());
    return clickPanel('hist');
  }).then(function(){
    add('--- 快照对比(来自 data/history) ---', '');
    add('navHistCnt', txt('navHistCnt'));
    add('histEmptyShown', !(document.getElementById('histEmpty') || {}).hidden);
    add('histBodyShown', !(document.getElementById('histBody') || {}).hidden);
    add('histKpis', document.querySelectorAll('#histKpis .hkpi').length);
    add('histDDs', document.querySelectorAll('#histFrom .dd, #histTo .dd').length);
    /* 下拉开合：点开 → 选项出现 → 点当前项 → 必须收起（点击冒泡的回归哨）。
       特意点「最后」一个选项 = 选中最新一张：histPick 排序后正好是最新一对，
       两张都有深索引，后面「变化下钻」的回归哨才有数据可钻。 */
    var hdd = document.querySelector('#histFrom .dd');
    if (hdd) hdd.click();
    return wait(300);
  }).then(function(){
    add('ddOpen', attrOf('#histFrom .dd', 'aria-expanded'));
    /* 快照删除：点垃圾桶必须只弹标准确认窗——单次点击绝不直接删除；
       Esc 关窗后一切如初。确认键不点（点了会真删用户的快照）。
       本段保持纯同步：弹窗开/关都是同步动作，这里不是 async 上下文。 */
    add('histDelBtns', document.querySelectorAll('#histFrom .ddx').length);
    var ddx0 = document.querySelector('#histFrom .ddx');
    if (ddx0) ddx0.click();
    add('histDelModal', document.querySelectorAll('.mmask').length);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    add('histDelModalClosed', document.querySelectorAll('.mmask').length);
    var opt = document.querySelector('#histFrom .ddopt:last-child');
    if (opt) opt.click();
    return wait(300);
  }).then(function(){
    add('ddClosedAfterPick', attrOf('#histFrom .dd', 'aria-expanded') === 'false'
        ? 'yes' : 'no');
    /* 「点其他区域必须收起」回归哨（用户报障：点外面收不走）。
       以 mousedown→mouseup→click 真实序列派发到外部目标，捕获关闭器必须生效；
       滚动页面时也要与原生 select 一致收起。 */
    return (async function(){
      var ddE = function(){
        var e = document.querySelector('#histFrom .dd');
        return e ? e.getAttribute('aria-expanded') : 'NA';
      };
      function seq(el){
        var r = el.getBoundingClientRect();
        var o = { bubbles: true, cancelable: true,
                  clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 };
        el.dispatchEvent(new MouseEvent('mouseover', o));
        el.dispatchEvent(new MouseEvent('mousedown', o));
        el.dispatchEvent(new MouseEvent('mouseup', o));
        el.dispatchEvent(new MouseEvent('click', o));
      }
      async function reopen(){
        var e = document.querySelector('#histFrom .dd');
        if (e) e.click();
        await wait(250);
      }
      var tc = document.querySelector('#histTrendWrap');
      tc = tc ? tc.closest('.card') : null;
      var title = tc ? tc.querySelector('h3') : null;
      await reopen();
      if (title) seq(title);
      await wait(250);
      add('ddClosedOutsideTitle', ddE() === 'false' ? 'yes' : 'no');
      await reopen();
      var row = document.querySelector('#histUp tr.hrow td');
      if (row) seq(row);
      await wait(250);
      add('ddClosedOutsideRow', ddE() === 'false' ? 'yes' : 'no');
      await reopen();
      var e2 = document.querySelector('#histFrom .dd');
      if (e2) e2.click();          // 点开关本身 → 收起
      await wait(250);
      add('ddClosedOnToggle', ddE() === 'false' ? 'yes' : 'no');
      await reopen();
      document.dispatchEvent(new Event('scroll'));
      await wait(250);
      add('ddClosedOnScroll', ddE() === 'false' ? 'yes' : 'no');
      /* 顶栏盘符：现在是个按钮，点开是本机磁盘弹窗。离线单文件没有服务端，
         点它只会提示，所以只在 http 下验。 */
      var chip = document.getElementById('driveChip');
      add('driveChip', chip ? chip.textContent.trim().slice(0, 24) : 'NONE');
      if (chip && location.protocol !== 'file:'){
        chip.click();
        await wait(700);
        add('driveDlgRows', document.querySelectorAll('.vmodal .vrow').length);
        add('driveDlgCur', document.querySelector('.vmodal .vrow.cur') ? 'FOUND' : 'NONE');
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        await wait(300);
        add('driveDlgClosed', document.querySelectorAll('.vmodal').length);
      }
    })();
  }).then(function(){
    add('histKpi.first', (function(){
      var e = document.querySelector('#histKpis .hkpi b');
      return e ? e.textContent.slice(0, 40) : 'NONE';
    })());
    add('histTrendCanvas', document.querySelectorAll('#histTrendWrap canvas').length);
    add('histUpRows', document.querySelectorAll('#histUp tr').length);
    add('histDownRows', document.querySelectorAll('#histDown tr').length);
    add('histNewRows', document.querySelectorAll('#histNew tr').length);
    add('histGoneRows', document.querySelectorAll('#histGone tr').length);
    add('overflow.hist', overflow('#p-hist'));
    return clickPanel('tree');   // 切走再切回，验证图表重挂
  }).then(function(){
    add('--- 回切目录结构 ---', '');
    add('treeRows.again', document.querySelectorAll('.trow').length);
    add('errCount', LOG.filter(function(l){ return l.indexOf('JSERR') === 0 ||
                                                   l.indexOf('REJECT') === 0; }).length);
    // 悬停趋势小图：合成 mouseover 走一遍真实 tooltip 路径（矩阵就绪才点）
    var hdbg = null;
    try { hdbg = window.__histDbg ? window.__histDbg() : null; } catch(e0){}
    add('histMatrixPaths', hdbg ? String(hdbg.matrixPaths) : 'NO_HOOK');
    var prow = (hdbg && hdbg.sparkReady) ? document.querySelector('.trow') : null;
    if (prow){
      prow.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, clientX: 60, clientY: 60 }));
      prow.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 60, clientY: 60 }));
    }
    return wait(300);
  }).then(function(){
    add('tipShown', document.querySelector('.tip.on') ? 'yes' : 'no');
    add('tipSpark', document.querySelectorAll('.tip .tspark').length);
    add('tipBaseDots', document.querySelectorAll('.tip .tsdot.base').length);
    add('tipChips', (function(){
      var e = document.querySelectorAll('.tip .hchip');
      var t = [];
      for (var i = 0; i < e.length; i++) t.push(e[i].textContent);
      return t.length ? t.join(' / ') : 'NONE';
    })());
    add('tipCompSegs', document.querySelectorAll('.tip .tipbar i').length);
    add('tipLegend', document.querySelectorAll('.tip .tipl span').length);
    add('tipActBtns', document.querySelectorAll('.tip .tipact .cpb button').length);
    /* 趋势按钮（第 4 颗，仅在有快照信息时出现）：点击应切到快照对比页 */
    var abtns = document.querySelectorAll('.tip .tipact .cpb button');
    if (abtns.length === 4) abtns[3].click();
    return wait(700);
  }).then(function(){
    var ph = document.getElementById('p-hist');
    add('tipGotoHist', ph ? (ph.classList.contains('on') ? 'yes' : 'no') : 'NO_PANEL');
    add('histCanvasAfterGoto', document.querySelectorAll('#histTrendWrap canvas').length);
    add('tipCap', (function(){
      var e = document.querySelector('.tip .ttc');
      return e ? e.textContent : 'NONE';
    })());
    // 深层下钻：涨跌榜第一个目录行的 ▸ 点开，应出现「变化构成」面板
    var dtg = document.querySelector('#histUp .htg');
    if (dtg) dtg.click();
    return wait(700);
  }).then(function(){
    add('deepPanel', document.querySelectorAll('#histUp tr.hdeep').length);
    add('deepRows', document.querySelectorAll('#histUp .ddeep-row').length);
    add('deepResidualRows', document.querySelectorAll('#histUp .ddeep-row.is-res').length);
    add('deepBarSegs', document.querySelectorAll('#histUp .ddeep-bar i').length);
    var ntg = document.querySelector('#histUp .ddeep-tg');
    if (ntg) ntg.click();
    return wait(300);
  }).then(function(){
    add('deepKidsRows', document.querySelectorAll('#histUp .ddeep-kids .ddeep-row').length);
    add('deepToggleState', (function(){
      var t = document.querySelector('#histUp .ddeep-tg');
      return t ? t.getAttribute('aria-expanded') : 'NO_TG';
    })());
  }).catch(function(e){
    // 链上任何一步抛错都不能让 __dbg 消失 —— 否则断在哪都查不到
    add('CHAINERR', (e && (e.stack || e.message)) || String(e));
  }).then(function(){
    flush();
  });
})();
</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="给 dist 里的单文件版插探针（用 file:// 验证离线路径）")
    ap.add_argument("src", nargs="?",
                    help="离线模式下指定要插探针的 html；缺省取 dist 里排序最后一个")
    a = ap.parse_args()

    if a.offline:
        # 只认正式产物：dist 里的 _probe_*.html / _*.txt 是中间文件，
        # 一旦把它们当成源，就会自己套自己（探针里还带着上一次的脚本）
        if a.src:
            if not os.path.basename(a.src).startswith("_") \
                    and os.path.exists(a.src):
                src = a.src
            else:
                print("[fatal] 指定的源不存在或是探针中间产物：%s" % a.src)
                return 1
        else:
            cands = [p for p in glob.glob(os.path.join(ROOT, "dist", "*.html"))
                     if not os.path.basename(p).startswith("_")]
            if not cands:
                print("[fatal] dist 里没有 html，先跑 scripts/build.py")
                return 1
            # 默认产物优先。dist 里可能还躺着别的盘的历史报告（xxx_K.html），
            # 按文件名排序时 "." 排在 "_" 前面，反而是旧报告落到最后 ——
            # 拿它当源就等于在一份过时的页面上跑检查，结论全是假的。
            main = os.path.join(ROOT, "dist", "磁盘空间分析报告.html")
            src = main if os.path.exists(main) else max(cands, key=os.path.getmtime)
        dst = os.path.join(ROOT, "dist", "_probe_offline.html")
    else:
        src = os.path.join(ROOT, "web", "index.html")
        dst = os.path.join(ROOT, "web", "_probe.html")

    if os.path.abspath(src) == os.path.abspath(dst):
        print("[fatal] 源与目标相同，拒绝自套：%s" % src)
        return 1

    html = io.open(src, encoding="utf-8").read()
    # 早期钩子必须在 <body> 之后、主脚本之前 —— 否则主脚本抛错时探针还没注册。
    # 用第一次出现的 <body>：真标签在前，echarts 的 saveAs-image 代码里虽有
    # 字面 '<body style=...>' 但在更后面。
    i = html.find("<body")
    if i < 0:
        print("[fatal] 找不到 <body>")
        return 1
    j = html.find(">", i) + 1
    html = html[:j] + EARLY + html[j:]
    # </body> 必须锚定「最后一次出现」：完整版 echarts 的字面量里有 '</body>'
    # （saveAs-image 往新窗口写的 HTML），replace 第一次出现会把探针插进图表
    # 代码中间，探针自带的 </script> 会提前终止图表脚本 —— 页面就此报废。
    k = html.rfind("</body>")
    if k < 0:
        print("[fatal] 找不到 </body>")
        return 1
    html = html[:k] + PROBE + html[k:]
    io.open(dst, "w", encoding="utf-8").write(html)
    print("[out] %s  (%s)" % (dst, os.path.basename(src)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
