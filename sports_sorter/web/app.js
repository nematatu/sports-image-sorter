const $ = (id) => document.getElementById(id);
let offset = 0, total = 0, csrf = '', items = [], requestId = 0;
const limit = 48, selected = new Set();
function message(text, error = false) { $('message').textContent = text; $('message').className = error ? 'error' : ''; }
function selection() { $('selected').textContent = `${selected.size}件選択`; $('save').disabled = selected.size === 0; }
function addText(parent, tag, text, cls) { const e = document.createElement(tag); e.textContent = text; if (cls) e.className = cls; parent.append(e); return e; }
function render() {
  $('grid').replaceChildren();
  for (const row of items) {
    const card = document.createElement('article'); card.className = 'card'; card.dataset.id = row.id;
    const picture = document.createElement('button'); picture.className = 'picture'; picture.setAttribute('aria-label', `画像 ${row.id} を拡大`);
    const img = document.createElement('img'); img.src = `/thumb/${row.id}`; img.alt = `画像 ${row.id}`; img.loading = 'lazy'; picture.append(img);
    picture.addEventListener('click', async () => {
      $('detail-image').src = `/preview/${row.id}`;
      $('detail-text').textContent = `画像 ${row.id} · ${row.label} · 推論 ${row.prediction_label || 'なし'} · ${row.evidence || ''} · 目視 ${row.review_evidence || '未入力'}`;
      $('detail').showModal();
      try {
        const response = await fetch(`/api/predictions/${row.id}`); const data = await response.json();
        if (response.ok && $('detail').open && $('detail-image').getAttribute('src') === `/preview/${row.id}`) {
          for (const p of data.items) addText($('detail-text'), 'span', ` | ${p.engine_key} → ${p.label} (${Number(p.score).toFixed(3)}): ${p.evidence}`);
        }
      } catch (e) { message(`比較結果の読み込みに失敗しました。${e.message}`, true); }
    });
    card.append(picture);
    const body = document.createElement('div'); body.className = 'card-body'; const top = document.createElement('div'); top.className = 'card-top';
    const check = document.createElement('input'); check.type = 'checkbox'; check.id = `image-${row.id}`; check.checked = selected.has(row.id); check.setAttribute('aria-label', `画像 ${row.id} を選択`);
    check.addEventListener('change', () => { check.checked ? selected.add(row.id) : selected.delete(row.id); card.classList.toggle('selected', check.checked); selection(); });
    top.append(check); const label = addText(top,'label',row.label); label.htmlFor = check.id;
    if (row.label === '未分類') addText(top,'span','要確認','badge uncertain');
    body.append(top);
    addText(body,'div',`#${row.id} · ${row.width || '?'} × ${row.height || '?'} · ${row.score == null ? 'スコアなし' : `スコア ${Number(row.score).toFixed(3)}`}`,'meta');
    if (row.disagreement) addText(body,'div','モデル間で判定が異なります','meta');
    if (row.review_label) addText(body,'div',row.human_verified ? '人による正解ラベルあり' : '修正済み・正解未確認','meta');
    if (row.error || row.decode_error) addText(body,'div',row.error || row.decode_error,'meta');
    card.append(body); card.classList.toggle('selected',check.checked); $('grid').append(card);
  }
  if (!items.length) addText($('grid'),'p','この条件に該当する画像はありません。');
  $('count').textContent = `${total}件`; $('page').textContent = total ? `${offset+1}–${Math.min(offset+limit,total)} / ${total}` : '0件';
  $('prev').disabled = offset === 0; $('next').disabled = offset + limit >= total; selection();
}
async function load() {
  const current = ++requestId;
  try {
    const query = new URLSearchParams({offset,limit,label:$('filter-label').value,mode:$('filter-mode').value});
    const response = await fetch(`/api/images?${query}`); const data = await response.json(); if (!response.ok) throw new Error(data.error); if (current !== requestId) return;
    items = data.items; total = data.total; csrf = data.csrf;
    const previous = $('filter-label').value; $('filter-label').replaceChildren(new Option('すべての競技','')); $('labels').replaceChildren();
    for (const label of data.labels) { $('filter-label').append(new Option(label,label)); $('labels').append(new Option(label,label)); }
    $('filter-label').value = previous;
    const s = data.summary; $('summary').textContent = `登録 ${s.total ?? s.images ?? '確認中'}件 · 正解ラベルは、人が画像を確認して保存した場合だけ評価に使います。`;
    render();
  } catch (e) { message(`読み込めませんでした。${e.message}`,true); }
}
for (const id of ['filter-label','filter-mode']) $(id).addEventListener('change',() => {offset=0;selected.clear();load();});
$('prev').addEventListener('click',() => {offset=Math.max(0,offset-limit);selected.clear();load();});
$('next').addEventListener('click',() => {offset+=limit;selected.clear();load();});
$('select-page').addEventListener('click',() => {items.forEach(r=>selected.add(r.id));render();});
$('clear').addEventListener('click',() => {selected.clear();render();});
$('close-detail').addEventListener('click',()=>$('detail').close());
$('save').addEventListener('click',async () => {
  $('save').disabled=true;
  try {
    const response=await fetch('/api/review',{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':csrf},body:JSON.stringify({ids:[...selected],label:$('review-label').value,evidence:$('evidence').value,human_verified:$('gold').checked})});
    const data=await response.json();if(!response.ok)throw new Error(data.error);
    message(`${data.updated}件の分類を保存しました。`);selected.clear();$('gold').checked=false;await load();
  }catch(e){message(`保存できませんでした。${e.message}`,true);selection();}
});
load();
