// 设置 → 运维：日志保留、用量保留、定价刷新、可信代理与更新检查。
import { html, live, nothing } from '../../core/dom.js';
import { toast, toastError } from '../../ui/toast.js';
import { parseNonNegativeInteger, parseNonNegativeNumber } from './logic.js';
import { envHint, numberRow, settingCard, switchRow, textRow } from './rows.js';
import { createUpdateBlock } from './update.js';

export function createMaintenanceTab(page) {
  const form = {
    pricingEnabled: true,
    pricingRefresh: '5',
    pricingVariant: true,
    logDb: false,
    logMaxRows: '',
    logMaxDays: '7',
    logMaxBytes: '',
    usageHourKeep: '48',
    usageDayKeep: '32',
    trustedProxies: '',
    updateCheck: false,
    updateCheckInterval: '24',
  };
  const errors = {
    pricingRefresh: '',
    logMaxRows: '', logMaxDays: '', logMaxBytes: '',
    usageHourKeep: '', usageDayKeep: '',
    updateCheckInterval: '',
  };
  let baseline = null;
  let saving = false;
  let dirtyShown = false;
  let envOverrides = {};
  const update = createUpdateBlock(page);

  function envFor(field) {
    if (field === 'usageHourKeep') return (envOverrides.usage || {}).hour_keep_hours || null;
    if (field === 'usageDayKeep') return (envOverrides.usage || {}).day_keep_days || null;
    if (field === 'trustedProxies') return envOverrides.trusted_proxies || null;
    if (field === 'updateCheck') return envOverrides.update_check_enabled || null;
    if (field === 'updateCheckInterval') return envOverrides.update_check_interval_hours || null;
    return envOverrides[field] || null;
  }

  function optionalNumber(value) {
    return value === null || value === undefined ? '' : String(value);
  }

  function stateOf(view) {
    const usage = view.usage || {};
    return {
      pricingEnabled: view.pricing_enabled !== false,
      pricingRefresh: String(Number(view.pricing_refresh_minutes ?? 5)),
      pricingVariant: view.pricing_variant_inherit !== false,
      logDb: view.log_db_enabled === true,
      logMaxRows: optionalNumber(view.log_db_max_rows),
      logMaxDays: optionalNumber(view.log_db_max_days),
      logMaxBytes: optionalNumber(view.log_db_max_bytes),
      usageHourKeep: String(Number(usage.hour_keep_hours ?? 48)),
      usageDayKeep: String(Number(usage.day_keep_days ?? 32)),
      trustedProxies: String(view.trusted_proxies || ''),
      updateCheck: view.update_check_enabled === true,
      updateCheckInterval: String(Number(view.update_check_interval_hours ?? 24)),
    };
  }

  function applyState(state) {
    Object.assign(form, state);
    for (const key of Object.keys(errors)) errors[key] = '';
    dirtyShown = false;
  }

  function seed(view) {
    baseline = stateOf(view);
    applyState(baseline);
    envOverrides = (view && view.env_overrides) || {};
  }

  function isDirty() {
    if (!baseline) return false;
    return Object.keys(form).some((key) => String(form[key]) !== String(baseline[key]));
  }

  function markDirty() {
    if (isDirty() === dirtyShown) return;
    dirtyShown = isDirty();
    page.draw();
  }

  function structChanged() {
    dirtyShown = isDirty();
    page.draw();
  }

  function revert() {
    if (!baseline) return;
    applyState(baseline);
    page.draw();
    toast.info('已撤销未保存的改动');
  }

  function numberText(value, label, { min = 0, max = 1e15, integer = true } = {}) {
    const parsed = integer
      ? parseNonNegativeInteger(value, { label, max })
      : parseNonNegativeNumber(value, { label, max });
    if (!parsed.ok) return { ok: false, error: parsed.error };
    if (parsed.value < min) return { ok: false, error: label + '不能小于 ' + min };
    return { ok: true, value: parsed.value };
  }

  async function saveAll() {
    const pricingRefresh = numberText(form.pricingRefresh, '定价刷新间隔', { max: 43200, integer: false });
    const logRows = form.logMaxRows === '' ? { ok: true, value: 0 }
      : numberText(form.logMaxRows, '日志行数上限', { max: 100000000 });
    const logDays = form.logMaxDays === '' ? { ok: true, value: 0 }
      : numberText(form.logMaxDays, '日志天数上限', { max: 36500 });
    const logBytes = form.logMaxBytes === '' ? { ok: true, value: 0 }
      : numberText(form.logMaxBytes, '日志大小上限', { max: 107374182400 });
    const usageHour = numberText(form.usageHourKeep, '小时层保留小时数', { min: 1, max: 336 });
    const usageDay = numberText(form.usageDayKeep, '日桶保留天数', { min: 1, max: 366 });
    const checkInterval = numberText(form.updateCheckInterval, '自动检查间隔', { min: 1, max: 720 });
    errors.pricingRefresh = pricingRefresh.ok ? '' : pricingRefresh.error;
    errors.logMaxRows = logRows.ok ? '' : logRows.error;
    errors.logMaxDays = logDays.ok ? '' : logDays.error;
    errors.logMaxBytes = logBytes.ok ? '' : logBytes.error;
    errors.usageHourKeep = usageHour.ok ? '' : usageHour.error;
    errors.usageDayKeep = usageDay.ok ? '' : usageDay.error;
    errors.updateCheckInterval = checkInterval.ok ? '' : checkInterval.error;
    if (!Object.values(errors).every((value) => !value)) {
      page.draw();
      return false;
    }
    saving = true;
    page.draw();
    const payload = {};
    const add = (key, field, value) => {
      if (!baseline || String(form[field]) === String(baseline[field])) return;
      payload[key] = value;
    };
    add('pricing_enabled', 'pricingEnabled', form.pricingEnabled);
    add('pricing_refresh_minutes', 'pricingRefresh', pricingRefresh.value);
    add('pricing_variant_inherit', 'pricingVariant', form.pricingVariant);
    add('log_db_enabled', 'logDb', form.logDb);
    add('log_db_max_rows', 'logMaxRows', logRows.value);
    add('log_db_max_days', 'logMaxDays', logDays.value);
    add('log_db_max_bytes', 'logMaxBytes', logBytes.value);
    add('trusted_proxies', 'trustedProxies', form.trustedProxies);
    add('update_check_enabled', 'updateCheck', form.updateCheck);
    add('update_check_interval_hours', 'updateCheckInterval', checkInterval.value);
    const usage = {};
    if (baseline && String(form.usageHourKeep) !== String(baseline.usageHourKeep)) {
      usage.hour_keep_hours = usageHour.value;
    }
    if (baseline && String(form.usageDayKeep) !== String(baseline.usageDayKeep)) {
      usage.day_keep_days = usageDay.value;
    }
    if (Object.keys(usage).length) payload.usage = usage;
    try {
      const view = await page.save(payload);
      baseline = stateOf(view);
      applyState(baseline);
      toast.success('运维设置已保存');
      // 开关或间隔变了，状态行要跟着刷新（是否启用、下次检查时间）。
      if ('update_check_enabled' in payload || 'update_check_interval_hours' in payload) {
        update.load();
      }
      return true;
    } catch (err) {
      toastError('保存运维设置失败', err);
      return false;
    } finally {
      saving = false;
      page.draw();
    }
  }

  // 字段行共用的表单状态：改动先写进草稿并清掉这一项的错误，再按翻转情况重绘。
  const ctl = {
    form,
    get saving() { return saving; },
    errors,
    env: envFor,
    change: (field, value) => {
      form[field] = value;
      if (field in errors) errors[field] = '';
      markDirty();
    },
  };

  /** 自动检查间隔：每天、每周或每隔若干小时，三种取值共用一个下拉加一个输入框。 */
  function intervalRow() {
    const hours = Number(form.updateCheckInterval);
    const preset = hours === 24 ? 'daily' : hours === 168 ? 'weekly' : 'custom';
    return html`
      <div class="setting">
        <div class="setting-main">
          <div class="setting-name">自动检查间隔</div>
          <div class="setting-desc">每天、每周或每隔若干小时检查一次。</div>
          ${envHint(envFor('updateCheckInterval'))}
          ${errors.updateCheckInterval ? html`<div class="field-error">${errors.updateCheckInterval}</div>` : nothing}
        </div>
        <div class="setting-control">
          <select class="select input-sm" .value=${preset} ?disabled=${saving}
                  aria-label="自动检查间隔"
                  @change=${(e) => {
                    const next = e.target.value;
                    form.updateCheckInterval = next === 'daily' ? '24'
                      : next === 'weekly' ? '168'
                      : (preset === 'custom' ? form.updateCheckInterval : '12');
                    errors.updateCheckInterval = '';
                    structChanged();
                  }}>
            <option value="daily" ?selected=${preset === 'daily'}>每天</option>
            <option value="weekly" ?selected=${preset === 'weekly'}>每周</option>
            <option value="custom" ?selected=${preset === 'custom'}>每隔</option>
          </select>
          ${preset === 'custom' ? html`
            <input class="input input-num" type="number" min="1" max="720" step="1"
                   aria-label="自动检查间隔小时数"
                   .value=${live(form.updateCheckInterval)} ?disabled=${saving}
                   @input=${(e) => { form.updateCheckInterval = e.target.value; errors.updateCheckInterval = ''; markDirty(); }}>
            <span class="setting-unit">小时</span>` : nothing}
        </div>
      </div>`;
  }

  return {
    seed,
    onMount() {
      update.load();
    },
    render() {
      return settingCard({
        iconName: 'Gauge',
        title: '运维',
        desc: '日志、用量聚合、定价刷新、可信代理与更新检查。',
        dirty: isDirty(),
        saving,
        onRevert: revert,
        onSave: saveAll,
        body: html`
          <div class="setting-list">
            <div class="section-label">定价与刷新</div>
            ${switchRow(ctl, {
              name: 'OpenRouter 价格估算',
              field: 'pricingEnabled',
              desc: '按 OpenRouter 价格估算每条请求的花费，关闭后按 0 计。',
            })}
            ${switchRow(ctl, {
              name: '价格变体后缀继承',
              field: 'pricingVariant',
              desc: '模型名带渠道或发行后缀时，从去掉后缀的基名继承价格。',
            })}
            ${numberRow(ctl, {
              name: '定价刷新间隔', field: 'pricingRefresh', suffix: '分钟',
              desc: '多久从 OpenRouter 拉取一次价格，0 表示关闭自动刷新。',
            })}

            <div class="section-label">运行日志</div>
            ${switchRow(ctl, {
              name: '运行日志写入数据库',
              field: 'logDb',
              desc: '运行日志写入 usage/logs.sqlite3，重启后仍可查看。',
            })}
            ${numberRow(ctl, {
              name: '日志行数上限', field: 'logMaxRows', suffix: '条',
              desc: '只保留最新 N 条日志，留空表示不按条数限制。',
            })}
            ${numberRow(ctl, {
              name: '日志天数上限', field: 'logMaxDays', suffix: '天',
              desc: '只保留最近 N 天日志，留空表示不按天数限制。',
            })}
            ${numberRow(ctl, {
              name: '日志大小上限', field: 'logMaxBytes', suffix: '字节',
              desc: '超过这个字节数时删除最旧的日志，留空表示不限制。',
            })}

            <div class="section-label">用量聚合</div>
            ${numberRow(ctl, {
              name: '小时层保留', field: 'usageHourKeep', suffix: '小时',
              desc: '小时分桶保留多久，默认 48 小时。',
            })}
            ${numberRow(ctl, {
              name: '日桶保留', field: 'usageDayKeep', suffix: '天',
              desc: '日分桶保留多少天，默认 32 天。',
            })}

            <div class="section-label">网络</div>
            ${textRow(ctl, {
              name: '可信反向代理', field: 'trustedProxies',
              placeholder: '例如 127.0.0.1,10.0.0.0/8',
              desc: '只有来自这些 IP 或 CIDR 的请求才读取 X-Real-IP 与 X-Forwarded-For。',
            })}

            <div class="section-label">更新</div>
            ${switchRow(ctl, {
              name: '自动检查更新',
              field: 'updateCheck',
              desc: '按下面的间隔检查 GitHub 上的新版本。',
            })}
            ${intervalRow()}
            ${update.render()}
          </div>`,
      });
    },
    async canLeave() {
      if (!isDirty()) return true;
      const choice = await page.confirmUnsaved({
        desc: '「运维」里的改动还没有保存。',
        onSave: saveAll,
      });
      return choice !== 'cancel';
    },
  };
}
