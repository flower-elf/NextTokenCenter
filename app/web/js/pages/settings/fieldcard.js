// 声明式设置卡片：按字段声明读取、校验、渲染与提交，卡片自带草稿、撤销与「有未保存的改动」标记。
// 渠道设置与通用设置都用它拼装，新增设置项只需要加一条字段声明。
//
// 字段声明：
//   id       表单里的字段名
//   key      设置接口的字段，嵌套字段写成 'upstream.header_timeout_seconds'
//   kind     'switch' | 'int' | 'number' | 'text' | 'select' | 'hours'
//   read     可选，从 GET /settings 取值的函数，缺省按 key 读取
//   default  服务端没有给值时的取值
//   options  select 的选项 [{ value, label }]，也可以是返回选项的函数
//   env      环境变量覆盖清单（GET /settings 的 env_overrides）里的键
//   validate 文本校验函数，返回错误文案，空串表示通过
//   whole    true 表示服务端按整个对象保存：同一对象里任一字段改动时，整组字段一起提交
import { html, live, nothing } from '../../core/dom.js';
import { toast, toastError } from '../../ui/toast.js';
import { hoursText, parseHours, parseNonNegativeInteger, parseNonNegativeNumber } from './logic.js';
import { envHint, numberRow, settingCard, switchRow, textRow } from './rows.js';

function getPath(obj, path) {
  return path.split('.').reduce((node, part) => (node == null ? undefined : node[part]), obj);
}

function setPath(obj, path, value) {
  const parts = path.split('.');
  let node = obj;
  for (const part of parts.slice(0, -1)) {
    node[part] = node[part] || {};
    node = node[part];
  }
  node[parts[parts.length - 1]] = value;
}

function readField(field, view) {
  const raw = field.read ? field.read(view || {}) : getPath(view || {}, field.key);
  if (field.kind === 'switch') return raw == null ? !!field.default : raw === true;
  if (field.kind === 'hours') return hoursText(raw == null ? field.default : raw);
  if (raw == null || raw === '') return String(field.default ?? '');
  return String(raw);
}

/** 校验一项草稿，返回 { ok, value, error }。 */
export function parseField(field, value) {
  if (field.kind === 'switch') return { ok: true, value: !!value };
  if (field.kind === 'hours') return parseHours(value, { label: field.label });
  if (field.kind === 'int' || field.kind === 'number') {
    const parse = field.kind === 'int' ? parseNonNegativeInteger : parseNonNegativeNumber;
    const parsed = parse(value, { label: field.label, max: field.max ?? Number.MAX_SAFE_INTEGER });
    if (!parsed.ok) return parsed;
    if (field.min != null && parsed.value < field.min) {
      return { ok: false, error: field.label + '不能小于 ' + field.min };
    }
    return parsed;
  }
  const text = String(value ?? '').trim();
  const error = field.validate ? field.validate(text) : '';
  return error ? { ok: false, error } : { ok: true, value: text };
}

/** 只提交改动过的字段；嵌套字段合并进同一个对象，whole 字段整组提交。maxOf 用于解析动态上限。 */
export function changedPayload(fields, form, baseline, maxOf = null) {
  const payload = {};
  const changed = (field) => String(form[field.id]) !== String(baseline[field.id]);
  const wholeGroups = new Set(fields.filter((field) => field.whole && changed(field))
    .map((field) => field.key.split('.')[0]));
  for (const field of fields) {
    const inWholeGroup = field.whole && wholeGroups.has(field.key.split('.')[0]);
    if (!inWholeGroup && !changed(field)) continue;
    const spec = maxOf ? { ...field, max: maxOf(field) } : field;
    setPath(payload, spec.key, parseField(spec, form[field.id]).value);
  }
  return payload;
}

export function createFieldCard(page, { icon, title, desc, sections, note }) {
  const fields = sections.flatMap((section) => section.fields);
  const form = {};
  const errors = {};
  let baseline = null;
  let env = {};
  let saving = false;
  let dirtyShown = false;
  // 服务端下发的上下限：字段的 max 可以写成 (view) => number，读取最近一次 seed 的视图。
  let lastView = {};

  function fieldMax(field) {
    const raw = typeof field.max === 'function' ? field.max(lastView || {}) : field.max;
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 ? value : Number.MAX_SAFE_INTEGER;
  }

  /** 输入框的 max 属性：没有声明上限时不写，避免每个数字框都带上一个天文数字。 */
  function maxAttr(field) {
    return field.max == null ? '' : String(fieldMax(field));
  }

  const isDirty = () => baseline !== null
    && fields.some((field) => String(form[field.id]) !== String(baseline[field.id]));

  const ctl = {
    form,
    errors,
    get saving() { return saving; },
    env: (id) => {
      const field = fields.find((item) => item.id === id);
      return field && field.env ? env[field.env] || null : null;
    },
    change(id, value) {
      form[id] = value;
      errors[id] = '';
      if (isDirty() !== dirtyShown) {
        dirtyShown = isDirty();
        page.draw();
      }
    },
  };

  function seed(view) {
    if (saving) return;
    lastView = view || {};
    baseline = Object.fromEntries(fields.map((field) => [field.id, readField(field, view)]));
    Object.assign(form, baseline);
    for (const field of fields) errors[field.id] = '';
    env = (view && view.env_overrides) || {};
    dirtyShown = false;
  }

  function revert() {
    if (!baseline) return;
    Object.assign(form, baseline);
    for (const field of fields) errors[field.id] = '';
    dirtyShown = false;
    page.draw();
  }

  async function save() {
    let valid = true;
    for (const field of fields) {
      const parsed = parseField({ ...field, max: fieldMax(field) }, form[field.id]);
      errors[field.id] = parsed.ok ? '' : parsed.error;
      if (!parsed.ok) valid = false;
    }
    if (!valid) {
      page.draw();
      return false;
    }
    saving = true;
    page.draw();
    try {
      const view = await page.save(changedPayload(fields, form, baseline, fieldMax));
      saving = false;
      seed(view);
      toast.success(title + '设置已保存');
      return true;
    } catch (err) {
      toastError('保存' + title + '设置失败', err);
      return false;
    } finally {
      saving = false;
      page.draw();
    }
  }

  function selectRow(field) {
    const options = typeof field.options === 'function'
      ? field.options(form[field.id], lastView) : field.options;
    const current = String(form[field.id] ?? '');
    const list = options.some((item) => item.value === current) || current === ''
      ? options : [...options, { value: current, label: current + '（当前设置）' }];
    return html`
      <div class="setting">
        <div class="setting-main">
          <div class="setting-name">${field.label}</div>
          <div class="setting-desc">${field.desc}</div>
          ${envHint(ctl.env(field.id))}
          ${errors[field.id] ? html`<div class="field-error">${errors[field.id]}</div>` : nothing}
        </div>
        <div class="setting-control">
          <select class="select input-sm select-wide" aria-label=${field.label} ?disabled=${saving}
                  .value=${live(current)} @change=${(event) => ctl.change(field.id, event.target.value)}>
            ${list.map((item) => html`<option value=${item.value} ?selected=${item.value === current}>${item.label}</option>`)}
          </select>
        </div>
      </div>`;
  }

  function row(field) {
    const spec = { name: field.label, desc: field.desc, field: field.id };
    if (field.kind === 'switch') return switchRow(ctl, spec);
    if (field.kind === 'int' || field.kind === 'number') {
      return numberRow(ctl, { ...spec, suffix: field.suffix || '', min: String(field.min ?? 0), max: maxAttr(field), step: String(field.step ?? 1) });
    }
    if (field.kind === 'select') return selectRow(field);
    return textRow(ctl, { ...spec, placeholder: field.placeholder || '' });
  }

  return {
    title,
    seed,
    save,
    isDirty,
    render() {
      return settingCard({
        iconName: icon,
        title,
        desc,
        dirty: isDirty(),
        saving,
        onRevert: revert,
        onSave: save,
        body: html`
          <div class="setting-list">
            ${sections.map((section) => html`
              ${section.label ? html`<div class="section-label">${section.label}</div>` : nothing}
              ${section.fields.map(row)}`)}
          </div>
          ${note ? note() : nothing}`,
      });
    },
  };
}

/** 一个设置标签由若干卡片组成：统一填值、渲染与离开前的未保存确认。 */
export function cardsTab(page, { name, cards, onMount }) {
  return {
    seed(view) {
      for (const card of cards()) card.seed(view);
    },
    render() {
      return html`${cards().map((card) => card.render())}`;
    },
    onMount,
    async canLeave() {
      const dirty = cards().filter((card) => card.isDirty());
      if (!dirty.length) return true;
      const choice = await page.confirmUnsaved({
        desc: '「' + name + '」里有改动还没有保存。',
        onSave: async () => (await Promise.all(dirty.map((card) => card.save()))).every(Boolean),
      });
      return choice !== 'cancel';
    },
  };
}
