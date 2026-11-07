/** API boundary. Synthetic fixtures are for UI rehearsal, never a real evaluation. */
const clone = value => structuredClone(value);
const encode = value => encodeURIComponent(String(value));
const round = value => Math.round((value + Number.EPSILON) * 100) / 100;

export class ApiError extends Error {
  constructor(message, status = 0) { super(message); this.name = 'ApiError'; this.status = status; }
}

// This public product definition is copied from data/checklists/v1.json.
const OFFICIAL_V1 = {"checklist_id":"freedom-chat-quality","version":1,"name":"Чек-лист качества чатов (Freedom Broker, хакатон)","official":true,"criteria":[{"id":"C1","section":"Начало и понимание запроса","title":"Установление контакта","answer_type":"binary","weight":10,"max_points":10,"description":"Да — оператор поздоровался на языке клиента и представился в первом ответе.","guidance":["Оценивается первый ответ оператора (реплики бота и системы — контекст). Язык приветствия должен совпадать с языком клиента (RU/KZ/EN)."],"scale":null,"scale_labels":null,"na_allowed":false,"na_rule":"","critical":false,"critical_when":null,"critical_rule":null,"hash":"905a6758922548ac"},{"id":"C2","section":"Начало и понимание запроса","title":"Выявление потребности","answer_type":"binary","weight":15,"max_points":15,"na_allowed":true,"description":"Да — оператор учёл известные детали и уточнил недостающее до ответа.","na_rule":"N/A — данных для ответа хватало с самого начала.","scale":null,"scale_labels":null,"critical":false,"critical_when":null,"critical_rule":null,"guidance":[],"hash":"f8da77b5dc03bf3e"},{"id":"C3","section":"Безопасность и решение","title":"Идентификация","answer_type":"binary","weight":20,"max_points":20,"na_allowed":true,"description":"Да — проверка личности подтверждена до раскрытия данных или защищённого действия.","na_rule":"N/A — запрос не требовал идентификации.","guidance":["Памятка (стр. 3): общий вопрос о тарифах не требует идентификации → N/A.","Памятка (стр. 3): если требовалась проверка личности перед защищённым действием и она не подтверждена в чате, ставьте «Нет»."],"scale":null,"scale_labels":null,"critical":false,"critical_when":null,"critical_rule":null,"hash":"16623d4c7a2483c7"},{"id":"C4","section":"Безопасность и решение","title":"Корректная и полная консультация","answer_type":"scale","weight":20,"max_points":20,"na_allowed":true,"scale":[0,5,10,15,20],"scale_labels":{"20":"Все существенные вопросы закрыты или дан корректный следующий шаг.","15":"Основной вопрос решён, пропущена второстепенная деталь.","10":"Часть запроса закрыта, существенный вопрос остался открытым.","5":"Есть общая подсказка, но клиенту всё ещё непонятно, как действовать.","0":"Ответа по существу нет или ключевая рекомендация ошибочна."},"description":"Оцените ответ или следующий шаг по шкале на стр. 2.","na_rule":"N/A — чат закончился до того, как клиент сформулировал запрос.","guidance":["Памятка (стр. 3): консультация оценивает, правильно и полно ли решён запрос. Ведение диалога (C6) оценивает, насколько понятно и последовательно оператор общался."],"critical":false,"critical_when":null,"critical_rule":null,"hash":"b9adb962c2da7d6e"},{"id":"C5","section":"Ведение и завершение диалога","title":"Эмпатия и клиентоориентированность","answer_type":"binary","weight":15,"max_points":15,"description":"Да — оператор вежлив и, если клиент выразил недовольство или описал неудобство, отреагировал на конкретную проблему.","scale":null,"scale_labels":null,"na_allowed":false,"na_rule":"","critical":false,"critical_when":null,"critical_rule":null,"guidance":[],"hash":"9f7fdff3bfa23020"},{"id":"C6","section":"Ведение и завершение диалога","title":"Управление диалогом, скорость и грамотность","answer_type":"scale","weight":15,"max_points":15,"scale":[0,4,7,10],"scale_labels":{"10":"Ответы ясные и связные. Если было явное ожидание, оператор предупредил.","7":"Один заметный сбой, но клиент может понять ответ и продолжить.","4":"Клиенту приходится переспрашивать из-за формулировок или порядка ответов.","0":"Из-за бессвязных или противоречивых реплик ход диалога непонятен."},"description":"Оцените ясность, порядок ответов и предупреждение об ожидании по шкале на стр. 2.","guidance":["Порог первой реакции 3 минуты, теги и вложения не оцениваются (стр. 2). Предупреждение клиента о явном ожидании в ходе диалога — часть C6 (ступень 10).","Памятка (стр. 3): ясный ответ на один из двух важных вопросов может получить высокий балл за ведение диалога и частичный за консультацию."],"na_allowed":false,"na_rule":"","critical":false,"critical_when":null,"critical_rule":null,"hash":"ce51f052c5719e8c"},{"id":"C7","section":"Ведение и завершение диалога","title":"Завершение","answer_type":"binary","weight":5,"max_points":5,"na_allowed":true,"description":"Да — оператор подвёл итог или предложил дальнейшую помощь и попрощался.","na_rule":"N/A — чат оборвался до возможности его завершить.","guidance":["Пример обоснования (стр. 3): «Завершение — Нет. После ответа на вопрос оператор закрыл чат без прощания. Основание — последняя реплика оператора и сообщение о закрытии чата»."],"scale":null,"scale_labels":null,"critical":false,"critical_when":null,"critical_rule":null,"hash":"36c9dba7fccf78f5"},{"id":"C8","section":"Грубые нарушения","title":"Грубые ошибки","answer_type":"binary","weight":0,"max_points":0,"critical":true,"critical_when":"yes","description":"Да — выявлено оскорбление клиента, предложение платной услуги в обход компании или раскрытие конфиденциальной информации. Нет — ни одно из трёх не выявлено.","critical_rule":"Без баллов. «Да» = нарушение выявлено (инвертированная полярность). Отмечается отдельным флагом, в итоговый балл не входит.","guidance":["Памятка (стр. 3): ставьте флаг, если оператор предлагает перевести деньги ему лично для инвестирования или просит личный номер клиента, чтобы оказать платную консультацию в частном порядке.","Памятка (стр. 3): достаточно самого предложения в переписке. Согласие клиента или перевод денег для отметки не требуются."],"scale":null,"scale_labels":null,"na_allowed":false,"na_rule":"","hash":"ae9b9e0074e5bc48"}],"sections":["Начало и понимание запроса","Безопасность и решение","Ведение и завершение диалога","Грубые нарушения"],"rounding":{"total":2,"mode":"half_up"},"scoring":{"yes":"полный вес","no":0,"scale":"points = value / max(scale) * weight (верхняя ступень = полный вес)","na":"критерии с N/A исключаются, их веса пропорционально перераспределяются между применимыми; максимум остаётся 100","no_applicable":"если применимых критериев с весом нет, итог не рассчитывается","critical":"критерий с critical=true отмечает грубую ошибку, если ответ == critical_when (C8: «Да» = нарушение); вес 0 — в итог не входит","procedure_pdf":["Вес показывает, сколько баллов критерий может дать до перераспределения при N/A.","1. Определите применимость. Ставьте N/A только там, где он разрешён и выполнено условие со стр. 1.","2. Выставьте оценку. «Да» даёт полный вес, «Нет» — ноль. Для шкального критерия выберите одну из указанных ступеней.","3. Пересчитайте шкальные оценки в баллы пропорционально весу. Верхняя ступень шкалы должна давать полный вес критерия.","4. Исключите критерии с N/A и пропорционально перераспределите их веса между применимыми критериями. Максимум должен остаться 100 баллов.","5. Округлите итог до двух знаков. Если применимых критериев с весом нет, итог не рассчитывается. Грубые ошибки отметьте отдельно.","В этом задании порог первой реакции в 3 минуты, теги и вложения в оценку не входят."]},"change_note":"Перенесено из Checklist.pdf организаторов без изменений формулировок; guidance — цитаты и пояснения со стр. 2–3","created_at":"2026-11-07","source":"data/raw/Checklist.pdf"};
const SEMANTIC_FIELDS = ['title','description','answer_type','scale','scale_labels','na_allowed','na_rule','critical','critical_when','critical_rule','guidance'];
const stable = value => Array.isArray(value) ? `[${value.map(stable).join(',')}]` : value && typeof value === 'object' ? `{${Object.keys(value).sort().map(k=>`${JSON.stringify(k)}:${stable(value[k])}`).join(',')}}` : JSON.stringify(value);
// Content identity only, not a cryptographic authentication mechanism. SHA-1
// matches the backend checklist contract and never includes weights or IDs.
function sha1(text) {
  const bytes = [...new TextEncoder().encode(text)], bits = bytes.length * 8;
  bytes.push(128); while (bytes.length % 64 !== 56) bytes.push(0);
  for (let i = 7; i >= 0; i--) bytes.push(Math.floor(bits / 2 ** (i * 8)) & 255);
  let h = [0x67452301,0xefcdab89,0x98badcfe,0x10325476,0xc3d2e1f0];
  const rotate = (v,n) => (v << n) | (v >>> (32-n));
  for (let offset=0; offset<bytes.length; offset+=64) {
    const w = Array(80);
    for(let i=0;i<16;i++) w[i]=(bytes[offset+i*4]<<24)|(bytes[offset+i*4+1]<<16)|(bytes[offset+i*4+2]<<8)|bytes[offset+i*4+3];
    for(let i=16;i<80;i++) w[i]=rotate(w[i-3]^w[i-8]^w[i-14]^w[i-16],1);
    let [a,b,c,d,e]=h;
    for(let i=0;i<80;i++) {
      const f=i<20?(b&c)|(~b&d):i<40?b^c^d:i<60?(b&c)|(b&d)|(c&d):b^c^d;
      const k=i<20?0x5a827999:i<40?0x6ed9eba1:i<60?0x8f1bbcdc:0xca62c1d6;
      const temp=(rotate(a,5)+f+e+k+w[i])|0;e=d;d=c;c=rotate(b,30);b=a;a=temp;
    }
    h=h.map((v,i)=>(v+[a,b,c,d,e][i])|0);
  }
  return h.map(v=>(v>>>0).toString(16).padStart(8,'0')).join('');
}
export function criterionHash(c) {
  const canon = {title:(c.title||'').trim(),description:(c.description||'').trim(),answer_type:c.answer_type||'binary',
    scale:c.scale?.map(Number)??null,scale_labels:c.scale_labels && Object.keys(c.scale_labels).length ? Object.fromEntries(Object.entries(c.scale_labels).map(([k,v])=>[String(Number(k)),String(v).trim()])) : null,
    na_allowed:!!c.na_allowed,na_rule:(c.na_rule||'').trim(),critical:!!c.critical,critical_when:c.critical?(c.critical_when||'yes'):null,
    critical_rule:(c.critical_rule||'').trim()||null,guidance:(c.guidance||[]).filter(x=>typeof x==='string'&&x.trim()).map(x=>x.trim())};
  return sha1(stable(canon)).slice(0,16);
}
export function makeChecklist(version = 1) {
  const checklist=clone(OFFICIAL_V1);
  checklist.version=version;checklist.active=version===1;
  if(version===2){const c=checklist.criteria.find(c=>c.id==='C4');c.title='Полнота ответа и проверяемый следующий шаг';c.description='Демонстрация замены: максимальный балл требует ответа на все вопросы и конкретного следующего шага.';c.hash=criterionHash(c);checklist.change_note='Демо: заменён один критерий C4';checklist.official=false;}
  return checklist;
}
// Exact rational arithmetic prevents binary floating-point ties changing half_up.
const gcd=(a,b)=>{while(b){[a,b]=[b,a%b];}return a<0n?-a:a;};
const q=(n,d=1n)=>{if(d<0n){n=-n;d=-d;}const g=gcd(n,d);return[n/g,d/g];};
const add=(a,b)=>q(a[0]*b[1]+b[0]*a[1],a[1]*b[1]);
const mul=(a,b)=>q(a[0]*b[0],a[1]*b[1]);
const div=(a,b)=>q(a[0]*b[1],a[1]*b[0]);
const number=a=>Number(a[0])/Number(a[1]);
const rational=value=>{const [mantissa,exponent='0']=String(value).toLowerCase().split('e');const digits=(mantissa.split('.')[1]||'').length-Number(exponent);const n=BigInt(mantissa.replace('.',''));return digits>=0?q(n,10n**BigInt(digits)):q(n*10n**BigInt(-digits));};
const roundedQ=(value,digits=2)=>{const factor=10n**BigInt(digits),n=value[0]*factor,d=value[1];const sign=n<0n?-1n:1n;const abs=n*sign;return Number(sign*((abs*2n+d)/(2n*d)))/Number(factor);};
const validAnswer=(c,a)=>a==='na'?!!c.na_allowed:c.answer_type==='binary'?['yes','no'].includes(a):typeof a==='number'&&Number.isFinite(a)&&(c.answer_type==='numeric'?a>=Math.min(...c.scale)&&a<=Math.max(...c.scale):c.scale.includes(a));
const criticalHit=(c,a)=>!!c.critical&&a===(c.critical_when||'yes');
export function scoreFor(id, checklist, evals) {
  const places=checklist.rounding?.total??2,byId=new Map(evals.map(e=>[e.criterion_id,e]));
  let applicable=q(0n),points=q(0n),complete=true;
  const lines=checklist.criteria.map(c=>{
    const ev=byId.get(c.id),answer=ev?.answer??null,is_na=answer==='na',weight=rational(c.weight);
    if(answer===null||!validAnswer(c,answer))complete=false;
    if(!is_na)applicable=add(applicable,weight);
    let earned=null;
    if(!is_na&&answer!==null&&validAnswer(c,answer)){
      const ratio=c.answer_type==='binary'?rational(answer==='yes'?1:0):div(rational(answer),rational(Math.max(...c.scale)));
      earned=mul(ratio,weight);points=add(points,earned);
    }
    return {criterion_id:c.id,title:c.title,section:c.section,answer,points:earned===null?null:number(earned),max_points:c.weight,weight:c.weight,weight_effective:0,contribution:is_na?0:null,is_na,critical:!!c.critical,critical_hit:criticalHit(c,answer),_earned:earned};
  });
  const applicableWeight=number(applicable),raw=complete&&applicableWeight>0?div(mul(points,q(100n)),applicable):null;
  for(const line of lines){if(!line.is_na&&applicableWeight>0){line.weight_effective=number(div(mul(rational(line.weight),q(100n)),applicable));line.contribution=line._earned===null?null:number(div(mul(line._earned,q(100n)),applicable));}}
  const sections=Object.fromEntries((checklist.sections||[...new Set(lines.map(l=>l.section))]).map(section=>{
    const group=lines.filter(l=>l.section===section&&!l.is_na),den=group.reduce((s,l)=>add(s,rational(l.weight)),q(0n));
    const sum=group.reduce((s,l)=>add(s,l._earned||q(0n)),q(0n));
    return[section,number(den)>0&&group.every(l=>l._earned!==null)?roundedQ(div(mul(sum,q(100n)),den),places):null];
  }));
  const na=lines.filter(l=>l.is_na).map(l=>l.criterion_id),earned=lines.filter(l=>l._earned!==null&&l.weight>0).map(l=>l.points);
  const total=raw===null?null:roundedQ(raw,places),criticalCriteria=lines.filter(l=>l.critical_hit).map(l=>l.criterion_id);
  return {conv_id:id,checklist_version:checklist.version,total,total_raw:raw===null?null:number(raw),sections,critical:!!criticalCriteria.length,critical_criteria:criticalCriteria,
    status:!complete?'incomplete':applicableWeight<=0?'no_applicable':'complete',weight_sum:checklist.criteria.reduce((s,c)=>s+c.weight,0),applicable_weight:applicableWeight,
    lines:lines.map(({_earned,...line})=>line),formula_steps:[`N/A: ${na.join(', ')||'нет'}`,`Сумма весов применимых: ${applicableWeight}`,`Баллы: ${earned.join(' + ')||'0'} = ${number(points)}`,raw===null?'Итог не рассчитывается: нет применимых критериев или оценка не завершена.':`${number(points)} / ${applicableWeight} × 100 = ${number(raw).toFixed(4)} → ${total}`],is_mock:true};
}
function demoEvaluation(detail,c,index=0,changed=false){
  const operators=detail.messages.filter(m=>m.speaker==='operator'&&m.text.trim());
  const defaults={C1:'yes',C2:index%3===0?'yes':'na',C3:'na',C4:[20,15,10,20,15][index%5],C5:'yes',C6:[10,7,4,10][index%4],C7:index%7===0?'no':'yes',C8:index%17===0?'yes':'no'};
  let answer=defaults[c.id];
  if(changed||!validAnswer(c,answer))answer=c.critical?'no':c.answer_type==='binary'?'yes':c.scale[Math.max(0,c.scale.length-2)];
  if(!operators.length)answer=c.na_allowed?'na':null;
  const message=operators[index%Math.max(1,operators.length)],confidence=index>=4&&index<13&&c.id==='C4'?.58:.92;
  const missing=answer==='no'&&!c.critical||answer===0;
  return {conv_id:detail.id,criterion_id:c.id,criterion_hash:c.hash,prompt_version:'synthetic-ui-v2',title:c.title,answer,
    evidence:message&&answer!==null&&answer!=='na'&&!missing?[{message_id:message.id,quote:message.text,start:0,end:message.text.length,valid:true}]:[],
    checked_range:missing&&operators.length?[operators[0].id,operators.at(-1).id]:null,na_reason:answer==='na'?`Демо: ${c.na_rule}`:null,
    explanation:'Демонстрационный ответ для проверки интерфейса. Это не результат оценки моделью.',suggestion:null,confidence,
    critical_hit:criticalHit(c,answer),model:'synthetic-ui',tokens_in:0,tokens_out:0,cost_kzt:0,latency_ms:0,from_cache:false,
    needs_review:!operators.length||confidence<.7||criticalHit(c,answer),review_reasons:confidence<.7?['Демонстрация низкой уверенности']:[],is_mock:true};
}

function makeDetail(index, checklist = makeChecklist(), idOverride) {
  const language = index < 35 ? 'RU' : index < 50 ? 'KZ' : 'EN';
  const channel = index >= 30 && index < 35 ? 'call' : 'chat';
  const id = idOverride || `demo-${language.toLowerCase()}-${String(index + 1).padStart(3, '0')}`;
  const examples = {
    RU: ['Пожалуйста, подождите. Подключаем оператора.', 'Как посмотреть историю операций?', 'Здравствуйте! Помогу найти историю операций.', 'В приложении откройте раздел «История». Выберите нужный период.', 'Спасибо, раздел найден.', 'Остались ли вопросы? Спасибо за обращение.'],
    KZ: ['Күте тұрыңыз, операторды қосамыз.', 'Операциялар тарихын қалай көруге болады?', 'Сәлеметсіз бе! Операциялар тарихын табуға көмектесемін.', 'Қосымшада «Тарих» бөлімін ашып, кезеңді таңдаңыз.', 'Рақмет, бөлімді таптым.', 'Тағы сұрағыңыз бар ма? Хабарласқаныңызға рақмет.'],
    EN: ['Please wait while we connect an operator.', 'How can I view my transaction history?', 'Hello! I can help you find your transaction history.', 'Open History in the app and select the period you need.', 'Thank you, I found it.', 'Do you have any other questions? Thank you for contacting us.'],
  };
  const roles = ['bot', 'client', 'operator', 'operator', 'client', 'operator'];
  const messages = examples[language].map((text, idx) => ({ id: `${id}:${idx}`, idx, speaker: roles[idx], text, ts_raw: `00:${String(idx * 9).padStart(2, '0')}`, t_sec: idx * 9, rec_sec: channel === 'call' ? idx * 9 : null }));
  const evals = checklist.criteria.map(criterion => demoEvaluation({id,messages},criterion,index));
  return { id, channel, language, group_id: 'SYNTHETIC', messages, meta: { handoff_idx: 2, wait_sec: 18, recording_gaps: [], lang_switches: [] }, audio_url: null,
    evals, score: scoreFor(id, checklist, evals), checklist: clone(checklist), appeals: [], decisions: [], audit: [], is_mock: true, mock_origin: 'built-in-synthetic' };
}

function summarize(detail) {
  return { id: detail.id, channel: detail.channel, language: detail.language, group_id: detail.group_id, total: detail.score.total, critical: detail.score.critical,
    status: !detail.evals.length?'pending':detail.score.status==='no_applicable'?'no_applicable':detail.score.status==='incomplete'?'incomplete':detail.evals.some(e=>e.needs_review)?'needs_review':'evaluated', needs_review: detail.evals.some(item => item.needs_review), cells: detail.score.lines.map(line => ({ ...line, confidence: detail.evals.find(e=>e.criterion_id===line.criterion_id)?.confidence, needs_review: detail.evals.find(e=>e.criterion_id===line.criterion_id)?.needs_review })), is_mock: true };
}

function validateChecklist(body) {
  if(!Array.isArray(body.criteria)||!body.criteria.length)throw new ApiError('Добавьте хотя бы один критерий.',422);
  const next=clone(body),ids=new Set();
  next.criteria=next.criteria.map(c=>{
    if(typeof c.id!=='string'||!c.id.trim()||ids.has(c.id))throw new ApiError('Идентификаторы критериев должны быть уникальными.',422);ids.add(c.id);
    if(!c.title?.trim()||!Number.isFinite(c.weight)||c.weight<0)throw new ApiError('Проверьте названия и веса критериев.',422);
    if(!['binary','scale','numeric'].includes(c.answer_type))throw new ApiError('Неизвестный тип ответа.',422);
    if(c.answer_type==='binary'){c.scale=null;c.scale_labels=null;}
    else if(!Array.isArray(c.scale)||!c.scale.length||c.scale.some(v=>!Number.isFinite(v)||v<0)||new Set(c.scale).size!==c.scale.length||Math.max(...c.scale)<=0)throw new ApiError('Задайте различные неотрицательные ступени шкалы.',422);
    if(c.critical&&c.answer_type!=='binary')throw new ApiError('Критерий грубой ошибки должен иметь тип «да/нет».',422);
    c.critical_when=c.critical?(c.critical_when||'yes'):null;
    if(c.critical&&!['yes','no'].includes(c.critical_when))throw new ApiError('Укажите ответ, означающий нарушение.',422);
    c.max_points=c.weight;c.hash=criterionHash(c);return c;
  });
  next.rounding=next.rounding||{total:2,mode:'half_up'};
  if(!Number.isInteger(next.rounding.total)||next.rounding.total<0||next.rounding.total>6||!['half_up',undefined].includes(next.rounding.mode))throw new ApiError('Округление: half_up, от 0 до 6 знаков.',422);
  next.sections=[...new Set([...(next.sections||[]),...next.criteria.map(c=>c.section).filter(Boolean)])];return next;
}
function checklistWarnings(cl){const sum=cl.criteria.reduce((s,c)=>s+c.weight,0);return [...(sum!==100?[`Сумма весов ${sum} ≠ 100 — итог нормируется к 100.`]:[]),...cl.criteria.filter(c=>c.na_allowed&&!c.na_rule?.trim()).map(c=>`${c.id}: N/A разрешён, но условие неприменимости не описано.`)];}
function checklistDiff(before,after){
  const hashes=new Set(before.criteria.map(c=>c.hash)),needs=[],reusable=[],rescore=[];
  const added=after.criteria.filter(c=>!before.criteria.some(old=>old.id===c.id));
  const removed=before.criteria.filter(c=>!after.criteria.some(next=>next.id===c.id));
  const changed=[];
  for(const c of after.criteria){const old=before.criteria.find(x=>x.id===c.id);const reeval=!hashes.has(c.hash);(reeval?needs:reusable).push(c.id);
    if(old){const fields=[...SEMANTIC_FIELDS,'weight','section'].filter(k=>stable(c[k]??null)!==stable(old[k]??null));if(fields.length){changed.push({criterion_id:c.id,fields,needs_reeval:old.hash!==c.hash,before:clone(old),after:clone(c)});if(old.hash===c.hash)rescore.push(c.id);}}}
  return {from:before.version,to:after.version,from_version:before.version,to_version:after.version,added,removed,changed,needs_reeval:needs,reusable,rescore_only:rescore,warnings:checklistWarnings(after),is_mock:true};
}
function finalEvals(detail,evals,checklist){
  const revised=clone(evals);
  for(const decision of detail.decisions||[]){const appeal=detail.appeals.find(a=>a.id===decision.appeal_id);const ev=revised.find(e=>e.criterion_id===(decision.criterion_id||appeal?.criterion_id));
    if(ev&&(!decision.criterion_hash||decision.criterion_hash===ev.criterion_hash))ev.answer=decision.new_answer;
  }
  for(const ev of revised){const c=checklist.criteria.find(c=>c.id===ev.criterion_id);ev.critical_hit=c?criticalHit(c,ev.answer):false;}return revised;
}
function normalizeUpload(payload,groupId){
  let values=payload?.data??payload;if(values?.records)values=values.records;if(values&&typeof values==='object'&&!Array.isArray(values)&&values.transcript)values=[values];
  if(!Array.isArray(values)||!values.length)throw new ApiError('JSON должен содержать непустой список диалогов.',422);
  const ids=new Set();
  return values.map(value=>{const id=String(value.conversation_id??value.id??'').trim();if(!id||ids.has(id))throw new ApiError('У каждого диалога должен быть уникальный идентификатор.',422);ids.add(id);
    if(!Array.isArray(value.transcript))throw new ApiError('В диалоге отсутствует массив transcript.',422);
    const messages=value.transcript.map((m,idx)=>{const speaker=String(m.speaker||'').toLowerCase();if(!['operator','client','bot','system'].includes(speaker)||typeof m.text!=='string')throw new ApiError('Проверьте роли и текст реплик.',422);
      return{id:`${id}:${idx}`,idx,speaker,text:m.text,ts_raw:String(m.timestamp??''),t_sec:null,rec_sec:null};});
    return{id,language:String(value.language||'RU').toUpperCase().replace(/^KK$/,'KZ'),channel:['call','voice call'].includes(value.channel)?'call':'chat',group_id:groupId,messages,meta:{recording_gaps:[]},audio_url:null,evals:[],score:{total:null,lines:[],sections:{},status:'unscored',critical:false},appeals:[],decisions:[],audit:[],is_mock:true,mock_origin:'uploaded-ui-demo'};
  });
}

export function createApi(CQA) {
  const config = CQA.config;
  const files = new Map();
  let lists = [makeChecklist(1), makeChecklist(2)];
  const details = new Map(Array.from({ length: 65 }, (_, i) => { const detail = makeDetail(i); return [detail.id, detail]; }));
  const appeals = new Map();
  const runs = new Map();
  let checklistsLoaded = false;
  let appealsLoaded = false;
  let activeVersion = 1;
  let sequence = 0;
  let rowCatalog = null;
  const settings = { mode: 'fast', location: 'cloud', profile: 'cloud' };
  const id = prefix => `${prefix}-${Date.now()}-${++sequence}`;
  const notifyMode = () => CQA.bus?.emit('api:mode', { mock: config.mock !== false, offline: !!config.offline });

  async function fetchJson(url, options = {}, responseType = 'json') {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Number(config.requestTimeout) || 12000);
    try {
      const response = await fetch(url, { ...options, signal: controller.signal, credentials: 'same-origin', headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers } });
      if (!response.ok) throw new ApiError(`Сервер вернул ошибку ${response.status}.`, response.status);
      if (response.status === 204) return null;
      return responseType === 'blob' ? await response.blob() : await response.json();
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(error.name === 'AbortError' ? 'Сервер не ответил вовремя. Повторите попытку.' : 'Не удалось получить данные. Проверьте подключение к серверу.');
    } finally { clearTimeout(timer); }
  }

  async function fixture(filename, fallback) {
    if (!config.mockBase) return typeof fallback === 'function' ? fallback() : clone(fallback);
    if (!files.has(filename)) {
      const base = new URL(config.mockBase.endsWith('/') ? config.mockBase : `${config.mockBase}/`, document.baseURI);
      files.set(filename, fetchJson(new URL(filename, base)).catch(error => { files.delete(filename); throw error; }));
    }
    return clone(await files.get(filename));
  }

  async function request(path, options, fallback) {
    if (config.mock !== false) { notifyMode(); return await fallback(); }
    if (config.offline) throw new ApiError('Офлайн-режим: операция требует подключения к серверу.');
    const base = new URL(`${String(config.apiBase || './api').replace(/\/$/, '')}/`, document.baseURI);
    try { return await fetchJson(new URL(path.replace(/^\//, ''), base), options); }
    catch (error) {
      // Fallback is opt-in and read-only. Real writes always fail visibly.
      if ((!options.method || options.method === 'GET') && config.fallbackToMock === true) {
        config.mock = true; notifyMode(); CQA.bus?.emit('api:fallback', { message: error.message }); return await fallback();
      }
      throw error;
    }
  }
  const get = (path, fallback) => request(path, {}, fallback);
  const post = (path, body, fallback) => request(path, { method: 'POST', body: JSON.stringify(body ?? {}) }, fallback);
  const query = fields => { const q = new URLSearchParams(); for (const [key, value] of Object.entries(fields || {})) if (value !== '' && value != null) q.set(key, value); return q.size ? `?${q}` : ''; };
  const getDetail = async key => {
    if (config.mockBase && !details.has(`loaded:${key}`) && !details.get(key)?.mock_origin?.startsWith('uploaded')) {
      const detail = await fixture(`conversation_${encode(key)}.json`, () => null);
      details.set(key, detail); details.set(`loaded:${key}`, true);
    }
    const value = details.get(key);
    if (!value || typeof value !== 'object') throw new ApiError('Диалог не найден.', 404);
    return value;
  };
  async function ensureAppeals() {
    if (appealsLoaded) return;
    const loaded = await fixture('appeals.json', []);
    for (const item of (Array.isArray(loaded) ? loaded : loaded.appeals || [])) {
      if (!appeals.has(item.id)) appeals.set(item.id, clone(item));
    }
    appealsLoaded = true;
  }
  const api = {
    conversations: (filters = {}) => get(`conversations${query(filters)}`, async () => {
      if(!rowCatalog){const result=await fixture('conversations.json',()=>[...details.values()].filter(v=>typeof v==='object').map(summarize));rowCatalog=Array.isArray(result)?result:result.conversations||result.items||[];}
      const rows=await Promise.all(rowCatalog.map(async row=>details.has(row.id)?summarize(await api.conversation(row.id)):row));
      return rows.filter(row => (!filters.lang || row.language === filters.lang) && (!filters.channel || row.channel === filters.channel) && (!filters.group_id || row.group_id === filters.group_id) && (filters.status !== 'needs_review' || row.needs_review));
    }),
    conversation: (key, options = {}) => get(`conversations/${encode(key)}${query(typeof options==='number'?{version:options}:options)}`, async () => {
      const detail=await getDetail(key),version=(typeof options==='number'?options:options.version)||activeVersion;
      if(!detail.checklist||Number(version)===detail.checklist.version)return clone(detail);
      const checklist=await api.checklist(version),saved=(detail.evaluation_history||[]).findLast(h=>h.checklist_version===Number(version));
      const pool=saved?.evals||detail.evals;
      const evals=checklist.criteria.flatMap(c=>{const ev=pool.find(e=>e.criterion_hash===c.hash);return ev?[{...clone(ev),criterion_id:c.id}]:[];});
      return {...clone(detail),checklist,evals,score:scoreFor(key,checklist,finalEvals(detail,evals,checklist))};
    }),
    checklists: () => get('checklists', async () => {
      if (!checklistsLoaded) {
        const result = await fixture('checklists.json', () => lists);
        lists = Array.isArray(result) ? result : result.versions || result.checklists || [];
        checklistsLoaded = true;
      }
      return clone(lists.map(c=>({...c,active:c.version===activeVersion})));
    }),
    checklist: version => get(`checklists/${encode(version)}`, async () => {
      if (config.mockBase) await api.checklists();
      const item = lists.find(value => String(value.version) === String(version));
      if (!item) throw new ApiError('Версия чек-листа не найдена.', 404);
      return {...clone(item),active:item.version===activeVersion};
    }),
    checklistDiff: (a, b) => get(`checklists/${encode(a)}/diff/${encode(b)}`, async () => {
      return checklistDiff(await api.checklist(a),await api.checklist(b));
    }),
    createChecklist: body => post('checklists', body, async () => {
      await api.checklists();const next=validateChecklist(body);
      const base=await api.checklist(body.base_version||body.parent_version||activeVersion);
      next.version=Math.max(0,...lists.map(c=>c.version))+1;next.created_at=new Date().toISOString();next.official=false;next.active=!!body.activate;
      lists.push(next);if(body.activate)activeVersion=next.version;
      return {...clone(next),diff:checklistDiff(base,next),warnings:checklistWarnings(next)};
    }),
    activateChecklist: version => post(`checklists/${encode(version)}/activate`, {}, async () => { await api.checklist(version); activeVersion = Number(version); return { version: activeVersion, active: true, is_mock: true }; }),
    lintChecklist: body => post('checklists/lint', body, async () => ({ warnings: !body.description?.trim() ? ['Добавьте точное описание условия выполнения.'] : [], is_mock: true })),
    createRun: body => post('runs', body, async () => {
      if(body.mode&&!['fast','accurate'].includes(body.mode))throw new ApiError('Выберите режим fast или accurate.',422);
      const version=await api.checklist(body.version||activeVersion),rows=await api.conversations({group_id:body.group_id});
      const ids=body.conv_ids||rows.map(r=>r.id);if(!ids.length)throw new ApiError('Нет диалогов для оценки.',422);
      for(const cid of ids)await getDetail(cid);
      const runId=id('demo-run');runs.set(runId,{id:runId,started:Date.now(),version,convIds:body.preview?ids.slice(0,5):ids,done:0,per_conv:[],preview:!!body.preview,activate:!!body.activate,activated:false,mode:body.mode||settings.mode,profile:body.profile||settings.profile,force:!!body.force,from_cache:0,simulated_evaluations:0});
      return {run_id:runId,is_mock:true};
    }),
    run: key => get(`runs/${encode(key)}`, async () => {
      const run=runs.get(key);if(!run)throw new ApiError('Демонстрационный прогон не найден.',404);
      const done=Math.min(run.convIds.length,Math.max(1,Math.floor((Date.now()-run.started)/20)));
      while(run.done<done){const convId=run.convIds[run.done],detail=await getDetail(convId),oldTotal=detail.score.total;
        const evals=run.version.criteria.map(c=>{const prior=detail.evals.find(e=>e.criterion_hash===c.hash&&(e.eval_mode||'fast')===run.mode&&(e.profile||'cloud')===run.profile);if(prior&&!run.force){run.from_cache++;return {...clone(prior),criterion_id:c.id,from_cache:true};}
          run.simulated_evaluations++;return {...demoEvaluation(detail,c,run.done,true),eval_mode:run.mode,profile:run.profile};});
        const next=scoreFor(convId,run.version,finalEvals(detail,evals,run.version));
        if(!run.preview){detail.evaluation_history||=[];detail.evaluation_history.push({checklist_version:detail.checklist?.version,checklist:clone(detail.checklist),evals:clone(detail.evals),score:clone(detail.score)});detail.evals=evals;detail.score=next;detail.checklist=clone(run.version);}
        run.per_conv.push({id:convId,old_total:oldTotal,new_total:next.total,status:'complete'});run.done++;
      }
      if(run.done===run.convIds.length&&run.activate&&!run.preview){activeVersion=run.version.version;run.activated=true;}
      return {run_id:key,activated:run.activated,version:run.version.version,done:run.done,total:run.convIds.length,from_cache:run.from_cache,cache_total:run.convIds.length*run.version.criteria.length,simulated_evaluations:run.simulated_evaluations,llm_calls:0,cost_kzt:0,elapsed:round((Date.now()-run.started)/1000),status:run.done===run.convIds.length?'complete':'running',per_conv:clone(run.per_conv),is_mock:true};
    }),
    runs: () => get('runs',async()=>Promise.all([...runs.keys()].map(key=>api.run(key)))),
    exportRun: async (key, format = 'json') => {
      if (!['json', 'csv'].includes(format)) throw new ApiError('Выберите формат JSON или CSV.', 422);
      if (config.mock !== false) throw new ApiError('Экспорт прогона доступен только при подключении к серверу.');
      if (config.offline) throw new ApiError('Офлайн-режим: экспорт требует подключения к серверу.');
      const base = new URL(`${String(config.apiBase || './api').replace(/\/$/, '')}/`, document.baseURI);
      return fetchJson(new URL(`runs/${encode(key)}/export${query({ format })}`, base),
        { headers: { Accept: format === 'csv' ? 'text/csv' : 'application/json' } }, 'blob');
    },
    config: () => get('config',async()=>({...settings,is_mock:true})),
    setConfig: body => post('config',body,async()=>{if(body.mode&&!['fast','accurate'].includes(body.mode))throw new ApiError('Некорректный режим.',422);if(body.location&&!['cloud','local'].includes(body.location))throw new ApiError('Некорректное размещение модели.',422);Object.assign(settings,body);if(body.location)settings.profile=body.location==='local'?'local':(body.profile||'cloud');return {...settings,is_mock:true};}),
    appeals: (filters = {}) => get(`appeals${query(filters)}`, async () => {
      await ensureAppeals();
      return clone([...appeals.values()].filter(value => (!filters.status || value.status === filters.status)&&(!filters.conv_id||value.conv_id===filters.conv_id)));
    }),
    createAppeal: body => post('appeals', body, async () => {
      if(!body.comment?.trim())throw new ApiError('Напишите комментарий к апелляции.',422);
      await ensureAppeals();
      const detail=await getDetail(body.conv_id),context=await api.conversation(body.conv_id,{version:body.checklist_version||activeVersion});
      const evaluation=context.evals.find(e=>e.criterion_id===body.criterion_id),criterion=context.checklist?.criteria.find(c=>c.id===body.criterion_id);
      if(!evaluation||evaluation.answer===null||!criterion)throw new ApiError('Критерий не найден в оценке.',422);
      if([...appeals.values()].some(a=>a.conv_id===body.conv_id&&a.criterion_hash===criterion.hash&&a.status==='open'))throw new ApiError('По критерию уже есть открытая апелляция.',409);
      if((body.message_ids||[]).some(mid=>!detail.messages.some(m=>m.id===mid)))throw new ApiError('Реплика не найдена в диалоге.',422);
      const appeal={...clone(body),id:id('demo-appeal'),status:'open',created_at:new Date().toISOString(),checklist_version:context.checklist.version,criterion_hash:criterion.hash,original_answer:evaluation.answer,current_answer:context.score.lines.find(l=>l.criterion_id===criterion.id)?.answer,is_mock:true};
      appeals.set(appeal.id,appeal);detail.appeals.push(appeal);return clone(appeal);
    }),
    dossier: key => get(`appeals/${encode(key)}/dossier`, async () => {
      await ensureAppeals();const appeal=appeals.get(key);if(!appeal)throw new ApiError('Апелляция не найдена.',404);
      const context=await api.conversation(appeal.conv_id,{version:appeal.checklist_version}),evaluation=context.evals.find(e=>e.criterion_id===appeal.criterion_id);
      return {appeal_id:key,status:'ready',arguments_for:[{text:'Демо: комментарий оператора требует проверки по указанным репликам.',message_ids:appeal.message_ids||[]}],
        arguments_against:[{text:'Демо: сопоставьте исходное обоснование с формулировкой критерия.',evidence:evaluation?.evidence||[]}],
        recommendation:'Демонстрационное досье. Решение принимает проверяющий.',is_mock:true};
    }),
    createDecision: body => post('decisions', body, async () => {
      if (!body.reason?.trim()) throw new ApiError('Укажите причину решения.', 422);
      if (!['confirm', 'change'].includes(body.action)) throw new ApiError('Выберите решение.', 422);
      await ensureAppeals();
      const appeal = appeals.get(body.appeal_id); if (!appeal) throw new ApiError('Апелляция не найдена.', 404);
      if (appeal.status === 'decided') throw new ApiError('Решение уже сохранено.', 409);
      const detail = await getDetail(appeal.conv_id);
      if (!detail.appeals.some(item => item.id === appeal.id)) detail.appeals.push(appeal);
      const pinned=await api.conversation(appeal.conv_id,{version:appeal.checklist_version}),active=await api.conversation(appeal.conv_id,{version:activeVersion});
      const evaluation=pinned.evals.find(e=>e.criterion_id===appeal.criterion_id),criterion=pinned.checklist.criteria.find(c=>c.id===appeal.criterion_id);
      const sameWording=active.checklist?.criteria.some(c=>c.hash===criterion.hash);
      const currentAnswer=(sameWording?active.score.lines.find(line=>line.criterion_id===criterion.id)?.answer:appeal.current_answer)??evaluation.answer;
      const answer = body.action === 'confirm' ? currentAnswer : body.new_answer;
      const valid = validAnswer(criterion,answer);
      if (!valid) throw new ApiError('Ответ не соответствует типу критерия.', 422);
      const oldTotal = active.score.total; const decision = { ...clone(body), id: id('demo-decision'), criterion_id:criterion.id, criterion_hash:criterion.hash, checklist_version:appeal.checklist_version, conv_id:appeal.conv_id, old_answer: currentAnswer, new_answer: answer, created_at: new Date().toISOString(), is_mock: true };
      detail.decisions.push(decision); appeal.status = 'decided';
      detail.score = scoreFor(detail.id, detail.checklist, finalEvals(detail,detail.evals,detail.checklist));
      const activeScore=scoreFor(detail.id,active.checklist,finalEvals(detail,active.evals,active.checklist));
      detail.audit.push({ id: detail.audit.length + 1, entity: 'decision', entity_id: decision.id, event: 'decision.saved', payload: { reason: body.reason }, ts: decision.created_at });
      return {old_total:oldTotal,new_total:activeScore.total,conv_id:detail.id,appeal:clone(appeal),decision:clone(decision),score:activeScore,warnings:sameWording?[]:['Критерий изменён: решение относится к прежней формулировке и не меняет текущий итог.'],is_mock:true};
    }),
    audio: key => get(`audio/${encode(key)}`, async () => fixture(`audio_${encode(key)}.json`, async () => { const detail = await getDetail(key); return { src: null, expires_at: null, gaps: detail.meta.recording_gaps || [], offsets: Object.fromEntries(detail.messages.filter(item => item.rec_sec != null).map(item => [item.id, item.rec_sec])), is_mock: true }; })),
    metrics: () => get('metrics', async () => {const rows=await api.conversations();return {conversations:rows.length,evaluated:rows.filter(r=>r.total!==null).length,cost_kzt:0,llm_calls:0,is_mock:true};}),
    modelGuide: async () => {
      // Static, historical measurements are distinct from the current demo run.
      const url = new URL(config.qualityReportUrl || './data/quality.json', document.baseURI);
      if (url.origin !== new URL(document.baseURI).origin) throw new ApiError('Файл справки должен находиться на том же сервере.');
      return fetchJson(url);
    },
    quality: async () => {
      // Quality is measured evidence, never generated from demo scores.
      if (config.mock !== false) return { reports: [] };
      if (config.offline) throw new ApiError('Офлайн-режим: замеры требуют подключения к серверу.');
      // The server has no metrics/quality route: read the published same-origin report first so a 404 never
      // reaches the browser console; the API endpoint is only a fallback for servers that do publish it.
      const reportUrl = new URL(config.qualityReportUrl || './data/quality.json', document.baseURI);
      if (reportUrl.origin !== new URL(document.baseURI).origin) throw new ApiError('Файл замеров должен находиться на том же сервере.');
      try { return await fetchJson(reportUrl); }
      catch (error) { if (error.status !== 404) throw error; }
      const base = new URL(`${String(config.apiBase || './api').replace(/\/$/, '')}/`, document.baseURI);
      try { return await fetchJson(new URL('metrics/quality', base)); }
      catch (error) { if (error.status === 404) return { reports: [] }; throw error; }
    },
    health: () => get('health', async () => ({ ok: true, provider: 'mock', model: 'synthetic-ui', offline: !!config.offline, is_mock: true })),
    ingest: (groupId = 'DEV', payload) => post(`ingest${query({group_id:groupId})}`, payload??null, async () => {
      await api.conversations();
      if(payload!=null){const imported=normalizeUpload(payload,groupId);for(const detail of imported){details.set(detail.id,detail);const i=rowCatalog.findIndex(r=>r.id===detail.id);if(i>=0)rowCatalog[i]=summarize(detail);else rowCatalog.push(summarize(detail));}return {count:imported.length,group_id:groupId,source:'upload',is_mock:true};}
      const rows=await api.conversations({group_id:groupId});if(!rows.length)throw new ApiError('В демо нет такого набора. Загрузите JSON или подключите сервер.',404);
      return {count:rows.length,group_id:groupId,source:'demo',is_mock:true};
    }),
  };
  return api;
}

export function mount(CQA = window.CQA) { CQA.api = createApi(CQA); return CQA.api; }
