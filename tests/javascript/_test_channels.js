/* 渠道登记表的渠道路由纯函数：同账号来源的互斥判断、勾选替换、按登记表顺序排列、显示文字。
 * 运行：node tests/javascript/_test_channels.js
 */
import assert from 'node:assert/strict';
import './_channels_fixture.js';
import { routeConflicts, toggleRouteChannel, routeText } from '../../app/web/js/core/channels.js';

// ---- 同 provider 互斥：intl 与 cn 都是 workbuddy，codearts 是另一个来源
assert.deepEqual(routeConflicts(['intl', 'codearts'], 'cn'), ['intl'], 'cn 与路由里的 intl 冲突');
assert.deepEqual(routeConflicts(['cn', 'codearts'], 'intl'), ['cn']);
assert.deepEqual(routeConflicts(['intl', 'codearts'], 'intl'), [], '自己不算在自己的冲突里');
assert.deepEqual(routeConflicts(['intl', 'codearts'], 'codearts'), [], '已选中的渠道不算冲突');
assert.deepEqual(routeConflicts([], 'intl'), []);

// ---- 勾选：没选过时加入；同来源的渠道已在路由里时替换；再点一次移除；结果按登记表顺序排列
assert.deepEqual(toggleRouteChannel([], 'intl'), ['intl']);
assert.deepEqual(toggleRouteChannel(['intl'], 'codearts'), ['intl', 'codearts'], '不同来源互不影响');
assert.deepEqual(toggleRouteChannel(['intl', 'codearts'], 'cn'), ['cn', 'codearts'], '替换同来源的渠道');
assert.deepEqual(toggleRouteChannel(['codearts'], 'cn'), ['cn', 'codearts'], '路由没有先后，按登记表顺序排列');
assert.deepEqual(toggleRouteChannel(['intl', 'codearts'], 'intl'), ['codearts'], '再次选中表示移除');
const original = ['intl', 'codearts'];
assert.notEqual(toggleRouteChannel(original, 'cn'), original);
assert.deepEqual(original, ['intl', 'codearts'], '不修改传入的路由');

// ---- 显示文字：参与调度的渠道用「 + 」连接，与选择先后无关；空路由用传入的文字
assert.equal(routeText([]), '');
assert.equal(routeText([], '跟随面板默认路由'), '跟随面板默认路由');
assert.equal(routeText(['cn']), '国内版');
assert.equal(routeText(['cn', 'codearts']), '国内版 + CodeArts');
assert.equal(routeText(['codearts', 'cn']), '国内版 + CodeArts', '与选择先后无关');

console.log('channels assertions passed');
