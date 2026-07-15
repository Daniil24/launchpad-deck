'use strict';
let M = {}, S = {}, ST = {}, mode = 'editor', tutIdx = 0, TUT = [];
const $ = id => document.getElementById(id);
const api = () => window.pywebview.api;

function tset(id, key){ const e = $(id); if (e) e.textContent = S[key] || ''; }

async function init(){
  try{ await api().prepare_main(); }catch(e){}   // hide + centre until the splash finishes
  M = await api().meta();
  S = M.s;
  ST = await api().state();
  applyStrings();
  buildGrid();
  buildSelects();
  renderEditor();
  fitPadFont();
  window.addEventListener('resize', fitPadFont);
  bind();
  setInterval(pollState, 700);
  requestAnimationFrame(previewLoop);
  if (!M.tutorial_seen) openTutorial();
}

function applyStrings(){
  document.documentElement.lang = M.lang;
  tset('subtitle','subtitle'); tset('tutBtnT','tutorial_btn');
  tset('lightBtnT','light_toggle'); tset('lightHint','light_hint');
  tset('profSec','profiles'); tset('profHint','prof_hint');
  tset('editorT','editor'); tset('previewT','preview');
  tset('editorHint','editor_hint'); tset('ctlHint','grid_ctl_hint');
  tset('lightSec','light_settings'); tset('sensT','sens'); tset('gainT','gain'); tset('brightT','bright');
  tset('bassT','bass'); tset('trebleT','treble');
  tset('lightSetHint','light_settings_hint');
  tset('sceneSec','scene_ctrl'); $('scPrev').textContent=S.scene_prev; $('scNext').textContent=S.scene_next;
  tset('scRandomT','scene_random'); tset('scPaletteT','scene_palette'); tset('scAutoT','scene_auto');
  tset('sceneHint','scene_hint');
  tset('langSec','language'); tset('autoSec','autostart'); tset('autoSwitchT','autostart_switch');
  tset('autoHint','autostart_hint'); tset('obsSec','obs_section'); tset('obsHint','obs_hint');
  $('obsPw').placeholder = S.obs_pass_ph || '';
  tset('plugSec','plugins_section'); tset('plugBtnT','plugins_open'); tset('plugHint','plugins_hint');
  tset('tutSec','tutorial_section'); tset('tutAgainT','tutorial_again');
  tset('rightsBadge','rights_badge'); tset('authorName','author_name');
  $('authorRights').textContent = S.author_rights; tset('tgBtnT','tg_btn'); tset('mailBtnT','mail_btn');
  tset('fName','f_name'); tset('fColor','f_color'); tset('fType','f_type'); tset('fParam','f_param');
  $('padSave').textContent = S.save; $('padCancel').textContent = S.cancel;
  $('tutBack').textContent = S.tut_back; $('tutSkip').textContent = S.tut_skip;
  // support (TON) + version
  tset('supTitle','sup_title'); tset('supDesc','sup_desc');
  $('copyTon').textContent = S.copy; tset('sendTonT','send_ton');
  $('tonAddr').textContent = M.ton_address || '';
  $('updateDl').textContent = S.download; $('updateLater').textContent = S.later;
  $('verTip').textContent = (S.ver_label || 'v') + ' ' + (M.version || '');
}

function buildSelects(){
  const col = $('padColor'); col.innerHTML = M.colors.map(c=>`<option>${c}</option>`).join('');
  const ty = $('padType'); ty.innerHTML = M.types.map(t=>`<option value="${t}">${S['type_'+t]}</option>`).join('');
  const ls = $('langSel'); ls.innerHTML = M.lang_order.map(c=>`<option value="${c}" ${c===M.lang?'selected':''}>${M.langs[c]}</option>`).join('');
  $('obsPw').value = M.obs_password || '';
  TUT = M.tut;
}

/* ---------- grid (adaptive: Launchpad Pro = 10x10, Mini/X = 9x9) ---------- */
function buildGrid(){
  const g = $('pgrid');
  const pro = M.device === 'pro';
  const LEFT = pro ? 1 : 0;
  const COLS = 8 + 1 + LEFT;                 // 8x8 + right control column (+ left macro column on Pro)
  const ROWS = 1 + 8 + (pro ? 1 : 0);        // top control row + 8 (+ bottom macro row on Pro)
  g.style.gridTemplateColumns = 'repeat(' + COLS + ',1fr)';
  g.innerHTML = '';
  for (let dr=0; dr<ROWS; dr++){
    for (let c=0; c<COLS; c++){
      const d = document.createElement('div'); d.className = 'cell';
      const gc = c - LEFT;                    // grid column 0..7 (when in range)
      if (dr===0){                            // top control row
        if (gc>=0 && gc<8) ctlCell(d, M.top_ctl[gc]);
        else if (c===COLS-1) logoCorner(d);   // top-right corner = logo (easter egg)
        else d.className = 'cell corner';
      } else if (dr<=8){                       // grid rows
        if (c===0 && pro) padCell(d, M.pro_left[dr-1]);          // left macro column (Pro)
        else if (gc>=0 && gc<8) padCell(d, gc + ',' + (8-dr));   // the 8x8 grid
        else if (c===COLS-1) ctlCell(d, M.right_ctl[dr-1]);      // right control column
        else d.className = 'cell corner';
      } else {                                // bottom macro row (Pro)
        if (gc>=0 && gc<8) padCell(d, M.pro_bottom[gc]);
        else d.className = 'cell corner';
      }
      g.appendChild(d);
    }
  }
}
function logoCorner(d){
  d.className = 'cell corner'; d.title = '🎵';
  d.innerHTML = '<img class="corner-logo" src="logo.png" alt="">';
  d.onclick = ()=>api().logo_egg();
}
function ctlCell(d, name){
  d.classList.add('ctl');
  if (!name){ d.classList.add('empty'); return; }
  if (name==='prev' || name==='next') d.classList.add('arrow');
  d.textContent = M.ctl_lbl[name];
  d.onclick = ()=>showCtl(name);
}
function padCell(d, key){
  d.classList.add('pad');
  d.dataset.key = key;
  d.dataset.note = key.charAt(0)==='o' ? key.slice(1)
                 : String(M_padIndex(+key.split(',')[0], +key.split(',')[1]));
  d.onclick = ()=>openPad(key);
}
function renderEditor(){
  document.querySelectorAll('.pad').forEach(d=>{
    const e = ST.layout[d.dataset.key];
    if (e){
      d.classList.add('set'); d.classList.remove('empty');
      d.style.background = M.chex[e.color] || '#666'; d.style.color = '#0b0b11';
      d.textContent = e.label || '';
    } else {
      d.classList.remove('set'); d.classList.add('empty');
      d.style.background = ''; d.style.color = ''; d.textContent = '';
    }
  });
}
function setMode(m){
  mode = m;
  $('modeEditor').classList.toggle('on', m==='editor');
  $('modePreview').classList.toggle('on', m==='preview');
  document.querySelectorAll('.pad').forEach(d=>d.style.pointerEvents = m==='editor' ? 'auto' : 'none');
  if (m==='editor') renderEditor();
}
async function previewLoop(){
  if (mode==='preview' && ST.running){
    try{
      const g = await api().grid();
      document.querySelectorAll('.pad').forEach(d=>{
        const hex = g[d.dataset.note] || '#0e0e16';
        d.style.background = hex; d.classList.remove('empty'); d.textContent='';
      });
    }catch(e){}
    await sleep(60);
  } else { await sleep(200); }
  requestAnimationFrame(previewLoop);
}
function fitPadFont(){
  const p = document.querySelector('.pad');
  if (!p) return;
  const w = p.clientWidth || 46;
  const fs = Math.max(7, Math.min(12.5, w * 0.2));
  document.documentElement.style.setProperty('--padfs', fs.toFixed(1) + 'px');
}
function M_padIndex(c, r){ return (r+1)*10 + (c+1); }
const sleep = ms => new Promise(r=>setTimeout(r, ms));

/* ---------- pad modal ---------- */
let curKey = null;
function openPad(key){
  curKey = key;
  const e = ST.layout[key] || {};
  $('padTitle').textContent = key.charAt(0)==='o'
      ? ('Кнопка ' + key.slice(1)) : ('Пэд ' + key.split(',').map(n=>+n+1).join(', '));
  $('padSub').textContent = S.dlg_sub;
  $('padLabel').value = e.label || '';
  $('padColor').value = e.color || 'green';
  $('padType').value = e.type || 'media';
  $('padParam').value = e.param || '';
  onTypeChange(); updateSwatch();
  $('padOverlay').classList.add('show');
}
function updateSwatch(){ $('padSwatch').style.background = M.chex[$('padColor').value] || '#888'; }
function onTypeChange(){
  const t = $('padType').value;
  const dl = $('paramList');
  dl.innerHTML = (M.suggest[t]||[]).map(s=>`<option value="${s}">`).join('');
  $('padHint').textContent = S['hint_'+t] || '';
  const noParam = M.no_param.includes(t);
  $('padParam').style.display = noParam ? 'none' : '';
  $('fParam').style.display = noParam ? 'none' : '';
  if (t==='media' && !(M.suggest.media||[]).includes($('padParam').value)) $('padParam').value='playpause';
}
async function padSaveFn(){
  const lbl = ($('padLabel').value.trim()) || $('padType').value;
  ST.layout[curKey] = {label:lbl, color:$('padColor').value, type:$('padType').value, param:$('padParam').value.trim()};
  await api().save_pad(curKey, ST.layout[curKey]);
  renderEditor(); closeOverlay('padOverlay');
}
async function padDelFn(){ delete ST.layout[curKey]; await api().delete_pad(curKey); renderEditor(); closeOverlay('padOverlay'); }
function closeOverlay(id){ $(id).classList.remove('show'); }

/* ---------- control info ---------- */
function showCtl(name){
  $('ctlGlyph').textContent = M.ctl_lbl[name];
  $('ctlName').textContent = S['ctl_'+name+'_t'] || name;
  $('ctlDesc').textContent = S['ctl_'+name+'_d'] || '';
  $('ctlNote').textContent = S.ctl_note;
  $('ctlOverlay').classList.add('show');
}

/* ---------- tutorial ---------- */
function openTutorial(){ tutIdx=0; renderTut(); $('tutOverlay').classList.add('show'); }
function renderTut(){
  const [emoji, kind, n] = TUT[tutIdx];
  $('tutIllus').textContent = emoji;
  $('tutTitle').textContent = S['tut'+n+'_t'] || '';
  $('tutBody').textContent = S['tut'+n+'_b'] || '';
  const ex = S['tut'+n+'_e'] || '';
  $('tutEx').style.display = ex ? '' : 'none'; $('tutEx').textContent = ex;
  $('tutDots').innerHTML = TUT.map((_,i)=>`<i class="${i===tutIdx?'on':''}"></i>`).join('');
  $('tutBack').style.visibility = tutIdx>0 ? 'visible' : 'hidden';
  $('tutNext').textContent = tutIdx===TUT.length-1 ? S.tut_done : S.tut_next;
}
function tutNextFn(){ if (tutIdx<TUT.length-1){ tutIdx++; renderTut(); } else { api().set_tutorial_seen(); closeOverlay('tutOverlay'); } }
function tutBackFn(){ if (tutIdx>0){ tutIdx--; renderTut(); } }
function tutSkipFn(){ api().set_tutorial_seen(); closeOverlay('tutOverlay'); }

/* ---------- state / status ---------- */
async function pollState(){
  ST = await api().state();
  const run = ST.running, want = ST.want;
  const cls = run?'run':(want?'wait':'');
  $('pulse').className = 'pulse '+cls;
  $('statusCard').classList.toggle('run', run);
  $('statusTxt').textContent = run?S.st_running:(want?S.st_searching:S.st_stopped);
  $('statusTxt').style.color = run?'var(--green)':(want?'var(--acc2)':'var(--mut)');
  $('startBtn').textContent = run?('⟳  '+S.restart):('▶  '+S.start);
  $('stopBtn').textContent = '■  '+S.stop;
  $('autoChk').checked = ST.autostart;
  if (ST.show_req) api().show_main();
  applyUpdate(ST.update);
  syncProfiles();
}
let _updShown = false;
function applyUpdate(u){
  const bar = $('updateBar');
  if (u && !u.dismissed){
    if (!_updShown){
      _updShown = true;
      $('updateText').textContent = S.upd_text + '  ·  v' + u.version;
      bar.style.display = 'flex';
      $('updateDl').onclick = ()=>api().open_url(u.url);
      $('updateLater').onclick = ()=>{ u.dismissed = true; bar.style.display = 'none'; };
    }
  } else if (!u){
    bar.style.display = 'none';
  }
}
function syncProfiles(){
  const sel = $('profSel');
  if (sel.dataset.sig !== JSON.stringify(ST.profiles)+ST.active){
    sel.dataset.sig = JSON.stringify(ST.profiles)+ST.active;
    sel.innerHTML = ST.profiles.map(p=>`<option ${p===ST.active?'selected':''}>${p}</option>`).join('');
  }
}
function setSlider(id, key){
  const inp = $(id), val = $(id+'V');
  const fill = ()=>{ const p=(inp.value-inp.min)/(inp.max-inp.min)*100;
    inp.style.background=`linear-gradient(to right, var(--acc) ${p}%, var(--card2) ${p}%)`; };
  inp.value = ST.light[key]; val.textContent = (+ST.light[key]).toFixed(2); fill();
  inp.oninput = ()=>{ val.textContent=(+inp.value).toFixed(2); ST.light[key]=+inp.value; fill(); api().set_light(key, +inp.value); };
}

/* ---------- bind ---------- */
function bind(){
  $('startBtn').onclick = async ()=>{ await api().start(); pollState(); };
  $('stopBtn').onclick = async ()=>{ await api().stop(); pollState(); };
  $('lightBtn').onclick = ()=>api().toggle_light();
  $('modeEditor').onclick = ()=>setMode('editor');
  $('modePreview').onclick = ()=>setMode('preview');
  setSlider('sens','sens'); setSlider('gain','gain'); setSlider('bright','bright');
  setSlider('bass','bass'); setSlider('treble','treble');
  $('scPrev').onclick = ()=>api().light_cmd('prev_scene');
  $('scNext').onclick = ()=>api().light_cmd('next_scene');
  $('scRandom').onclick = ()=>api().light_cmd('random_scene');
  $('scPalette').onclick = ()=>api().light_cmd('palette');
  $('scAuto').onclick = ()=>api().light_cmd('toggle_auto');
  $('padColor').onchange = updateSwatch;
  $('padType').onchange = onTypeChange;
  $('padSave').onclick = padSaveFn; $('padDelete').onclick = padDelFn;
  $('padCancel').onclick = ()=>closeOverlay('padOverlay');
  $('ctlOk').onclick = ()=>closeOverlay('ctlOverlay');
  $('tutBtn').onclick = openTutorial; $('tutAgain').onclick = openTutorial;
  $('tutNext').onclick = tutNextFn; $('tutBack').onclick = tutBackFn; $('tutSkip').onclick = tutSkipFn;
  $('autoChk').onchange = ()=>api().set_autostart($('autoChk').checked);
  $('langSel').onchange = async ()=>{ await api().set_lang($('langSel').value); location.reload(); };
  $('obsPw').oninput = ()=>api().set_obs_password($('obsPw').value);
  $('plugBtn').onclick = ()=>api().open_plugins();
  $('tgBtn').onclick = ()=>api().open_url('https://t.me/universemusicrecords');
  $('mailBtn').onclick = ()=>api().open_url('mailto:doskin50@gmail.com');
  $('copyTon').onclick = async ()=>{
    try{ await navigator.clipboard.writeText(M.ton_address); }catch(e){}
    $('copyTon').textContent = S.copied;
    setTimeout(()=>{ $('copyTon').textContent = S.copy; }, 1400);
  };
  $('sendTon').onclick = ()=>api().open_url(M.ton_link);
  $('profNew').onclick = async ()=>{ const n = prompt(S.prof_name_q); if(n){ ST = await api().new_profile(n); renderEditor(); syncProfiles(); } };
  $('profRen').onclick = async ()=>{ const n = prompt(S.prof_name_q, ST.active); if(n){ ST = await api().rename_profile(n); syncProfiles(); } };
  $('profDel').onclick = async ()=>{ ST = await api().delete_profile(); renderEditor(); syncProfiles(); };
  $('profSel').onchange = async ()=>{ ST = await api().switch_profile($('profSel').value); renderEditor(); };
  document.querySelectorAll('.overlay').forEach(o=>o.onclick = e=>{ if(e.target===o) o.classList.remove('show'); });
}

if (window.pywebview) init();
else window.addEventListener('pywebviewready', init);
