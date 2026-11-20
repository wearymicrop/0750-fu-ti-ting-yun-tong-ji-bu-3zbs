import {$, cards, el, label, num, stamp, table} from './format.mjs';
import {request} from './api.mjs';

const secs = value => `${num(value)} 秒`;
let mountedKey = null;

function summaryRows(record) {
  const {settled, carry_over: carry, conservation} = record;
  return [
    ['本班已核营业', secs(settled.service_seconds), '可证实时段，已落账'],
    ['本班责任停运', secs(settled.responsible_seconds), label('responsible')],
    ['本班豁免', secs(settled.exempt_seconds), label('exempt')],
    ['本班可用', secs(settled.available_seconds), label('available')],
    ['本班缺证', secs(settled.missing_seconds), label('missing')],
    ['下一班承接营业', secs(carry.service_seconds), '跨交接点的营业余量'],
    ['其中缺证余量', secs(carry.missing_seconds), '含未恢复/未确认报码'],
    ['未确认余量', secs(carry.unconfirmed_seconds), '恢复后由下一班补证'],
    ['守恒差', secs(conservation.difference), '已结+承接须等于营业并集'],
  ];
}

function renderHandover(record) {
  cards($('handover-kpis'), [
    ['本班已核时长', secs(record.settled.service_seconds)],
    ['下一班承接余量', secs(record.carry_over.service_seconds), 'accent'],
    ['缺证余量', secs(record.carry_over.missing_seconds)],
    ['守恒差', secs(record.conservation.difference)],
  ]);
  table($('handover-summary'), [['项目'], ['秒数', true], ['说明']], summaryRows(record));
  const devices = new Set([
    ...Object.keys(record.settled.by_device), ...Object.keys(record.carry_over.by_device),
  ]);
  table($('handover-devices'),
    [['设备'], ['本班已结', true], ['结转余量', true], ['其中缺证', true]],
    [...devices].sort().map(device => [
      device,
      num(record.settled.by_device[device]?.service_seconds ?? 0),
      num(record.carry_over.by_device[device]?.service_seconds ?? 0),
      num(record.carry_over.by_device[device]?.missing_seconds ?? 0),
    ]));
  table($('handover-carry'),
    [['设备'], ['报码'], ['原因'], ['状态'], ['本班已结', true], ['承接余量', true], ['未确认', true], ['采用报码']],
    record.carry_over.events.map(row => [
      row.device, row.id, label(row.reason), label(row.state),
      num(row.settled_seconds), num(row.carried_seconds), num(row.unconfirmed_seconds),
      row.codes.map(code => label(code)).join('、'),
    ]));
  table($('handover-codes'),
    [['采用报码'], ['责任', true], ['豁免', true], ['缺证', true], ['合计', true]],
    record.adopted_codes.map(row => [
      label(row.code), num(row.responsible), num(row.exempt), num(row.missing),
      num(row.responsible + row.exempt + row.missing),
    ]));
  $('handover-permits').replaceChildren();
  $('handover-permits').previousElementSibling?.remove();
  if (record.permits.length) {
    $('handover-permits').before(el('h3', '采用审批（已批准）'));
    table($('handover-permits'),
      [['行号', true], ['审批单'], ['设备'], ['原因'], ['凭据'], ['覆盖时段']],
      record.permits.map(row => [
        row.line, row.id, row.device, label(row.reason), row.approval,
        `${stamp(row.start)} → ${stamp(row.end)}`,
      ]));
  }
}

function fillBoundaries(boundaries, selected) {
  const select = $('handover-at');
  select.replaceChildren(...boundaries.map(point => {
    const option = el('option', stamp(point));
    option.value = String(point);
    return option;
  }));
  if (selected != null) select.value = String(selected);
}

function renderArchive(handovers, onOpen) {
  table($('handover-archive'),
    [['交接时刻'], ['已结秒数', true], ['承接余量', true], ['缺证余量', true], ['记录']],
    handovers.map(item => {
      const button = el('button', item.id.slice(0, 12), 'mono');
      button.type = 'button';
      button.addEventListener('click', () => onOpen(item));
      return [stamp(item.handover_at), num(item.record.settled.service_seconds),
        num(item.record.carry_over.service_seconds), num(item.record.carry_over.missing_seconds), button];
    }));
}

function emptyPanels(message) {
  $('handover-kpis').replaceChildren();
  ['handover-summary', 'handover-devices', 'handover-carry', 'handover-codes',
    'handover-permits', 'handover-archive']
    .forEach(id => $(id).replaceChildren());
  $('handover-feedback').textContent = message;
}

// 后端按整秒时间戳解析，前端统一传时间戳，避免时区文本歧义
const previewAt = (sample, point) =>
  request(`/api/handover/preview/${sample}${point === '' ? '' : `?at=${point}`}`);

export function resetHandover() {
  mountedKey = null;
}

export async function mountHandover(state) {
  const select = $('handover-at');
  const settleButton = $('handover-settle');
  const feedback = $('handover-feedback');
  const key = state.report ? `report:${state.report.id}` : `sample:${state.sample}`;
  if (key === mountedKey) return;
  mountedKey = key;
  feedback.textContent = '';
  select.onchange = null;
  settleButton.onclick = null;

  if (state.report && state.report.manifest.source !== 'custom') {
    // 归档样例：试算用同源样例，存档走留档输入重算
    const {id: reportId} = state.report;
    const sample = state.report.manifest.source;
    $('handover-mode').textContent = '归档模式 · 结班存档随班报留痕，同一交接点重复提交不会重复结转';
    settleButton.hidden = false;
    settleButton.disabled = false;
    const [handovers, first] = await Promise.all([
      request(`/api/runs/${reportId}/handovers`).then(data => data.handovers),
      previewAt(sample, ''),
    ]);
    fillBoundaries(first.boundaries);
    const archived = handovers.find(item => String(item.handover_at) === select.value);
    renderHandover(archived ? archived.record : first.record);
    renderArchive(handovers, item => {
      select.value = String(item.handover_at);
      renderHandover(item.record);
    });
    select.onchange = async () => {
      const detail = await previewAt(sample, select.value);
      renderHandover(detail.record);
    };
    settleButton.onclick = async () => {
      settleButton.disabled = true;
      try {
        const saved = await request(`/api/runs/${reportId}/handovers`, {at: Number(select.value)});
        feedback.textContent = '结班已存档，余量已结转下一班';
        const mine = saved.handovers.find(item => String(item.handover_at) === select.value);
        if (mine) renderHandover(mine.record);
        renderArchive(saved.handovers, item => {
          select.value = String(item.handover_at);
          renderHandover(item.record);
        });
      } catch (error) {
        feedback.textContent = error.message;
      } finally {
        settleButton.disabled = false;
      }
    };
  } else if (state.report) {
    // 自定义导入班报：无样例可试算，仅展示已留痕的交接记录
    $('handover-mode').textContent = '归档模式 · 自定义班报，展示已留痕的交接记录';
    settleButton.hidden = true;
    const handovers = (await request(`/api/runs/${state.report.id}/handovers`)).handovers;
    fillBoundaries(handovers.map(item => item.handover_at));
    if (handovers.length) {
      select.value = String(handovers[0].handover_at);
      renderHandover(handovers[0].record);
    } else {
      emptyPanels('暂无交接记录');
    }
    renderArchive(handovers, item => {
      select.value = String(item.handover_at);
      renderHandover(item.record);
    });
  } else {
    // 试算模式：不落档
    $('handover-mode').textContent = '试算模式 · 数据未归档，仅展示不写库';
    settleButton.hidden = true;
    const first = await previewAt(state.sample, '');
    fillBoundaries(first.boundaries);
    renderHandover(first.record);
    select.onchange = async () => renderHandover((await previewAt(state.sample, select.value)).record);
  }
}
