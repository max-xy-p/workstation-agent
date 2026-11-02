import { expect, test } from '@playwright/test';
import type { Conversation, Message } from '../src/api/contracts';

test('演示会话可以更名、补充背景，编辑时明确清除后续消息', async ({ page }) => {
  await page.goto('/?demo=1');
  await expect(page.getByRole('heading', { name: '前端工作台设计', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '更改会话标题' }).click();
  await page.getByLabel('会话标题', { exact: true }).fill('页面与接口');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.getByRole('heading', { name: '页面与接口', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '编辑消息', exact: true }).first().click();
  await expect(page.getByText('保存后会删除这条消息之后的全部消息', { exact: false })).toBeVisible();
  await page.getByLabel('消息内容').fill('请先给出页面布局。');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.getByText('请先给出页面布局。', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '把会话放在中心' })).toHaveCount(0);
  await page.getByRole('button', { name: '补充背景', exact: true }).click();
  await page.getByLabel('消息内容').fill('后端已经使用 PostgreSQL。');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.getByText('后端已经使用 PostgreSQL。', { exact: true })).toBeVisible();
});

test('审批必须选择全部决定，审批期间禁止编辑历史', async ({ page }) => {
  await page.goto('/?demo=1');
  await page.getByRole('button', { name: '把项目文档加入知识库', exact: true }).click();
  await expect(page.getByRole('heading', { name: '需要你的决定' })).toBeVisible();
  await expect(page.getByRole('button', { name: '提交决定' })).toBeDisabled();
  await expect(page.getByRole('button', { name: '编辑消息', exact: true })).toBeDisabled();
  await page.getByRole('radio', { name: '拒绝', exact: true }).check();
  await page.getByRole('button', { name: '提交决定' }).click();
  await expect(page.getByText('演示审批已处理。真实连接下，Agent 会按你选择的决定继续执行。')).toBeVisible();
});

test('真实 HTTP 客户端使用读取版本，冲突后不盲目覆盖，令牌不落浏览器存储', async ({ page }) => {
  const date = new Date().toISOString();
  const chat: Conversation = { id: 'session_contract', title: '接口会话', revision: 4, created_at: date, updated_at: date };
  const message: Message = { id: 'message_one', session_id: chat.id, role: 'user', content: '保留的原文',
    position: 1, run_id: null, created_at: date, updated_at: date };
  const writes: { revision: number; content: string }[] = [];
  const token = 'test_frontend_only_' + 'a'.repeat(32);
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    if (!new URL(request.url()).pathname.startsWith('/api/')) { await route.continue(); return; }
    expect(request.headers().authorization).toBe(`Bearer ${token}`);
    const path = new URL(request.url()).pathname;
    let body: unknown;
    if (path.endsWith('/messages/message_one') && request.method() === 'PATCH') {
      const data = request.postDataJSON() as { revision: number; content: string };
      writes.push(data);
      await route.fulfill({ status: 409, json: { error: 'revision_conflict', message: '会话已变化，请重新读取后修改' } });
      return;
    }
    if (path === '/api/sessions') body = { items: [chat], next_offset: null };
    else if (path.endsWith('/messages')) body = { items: [message], next_offset: null };
    else if (path.endsWith('/approvals')) body = {};
    else if (path === '/api/runs') body = { items: [], next_offset: null };
    else body = chat;
    await route.fulfill({ json: body });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '连接本机 Agent', exact: true }).click();
  await page.getByLabel('访问令牌', { exact: true }).fill(token);
  await page.getByRole('button', { name: '连接服务', exact: true }).last().click();
  await expect(page.getByText('保留的原文', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '编辑消息', exact: true }).click();
  await page.getByLabel('消息内容').fill('准备修改');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('会话已变化');
  expect(writes).toEqual([{ revision: 4, content: '准备修改' }]);
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 });
  expect(page.url()).not.toContain(token);
});

test('窄屏支持会话导航，主页面没有横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?demo=1');
  await expect(page.getByRole('heading', { name: '前端工作台设计', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '收起运行详情', exact: true }).click();
  await page.getByRole('button', { name: '展开会话导航', exact: true }).click();
  await expect(page.getByRole('button', { name: '新建会话', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '收起会话导航', exact: true }).click();
  const navigation = await page.locator('.sidebar').boundingBox();
  expect(navigation && navigation.x + navigation.width).toBeLessThanOrEqual(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('提交任务后可取消，不重复插入用户消息', async ({ page }) => {
  await page.goto('/?demo=1');
  await page.getByRole('button', { name: '新建会话', exact: true }).click();
  await page.getByRole('textbox', { name: '任务内容' }).fill('检查运行取消');
  await expect(page.getByRole('button', { name: '发送任务', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '发送任务', exact: true }).click();
  await page.getByRole('button', { name: '停止运行', exact: true }).click();
  await expect(page.getByRole('button', { name: '停止运行', exact: true })).toHaveCount(0);
  await expect(page.getByRole('article')).toHaveCount(1);
});

test('保存桌面预览', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/?demo=1');
  await expect(page.getByRole('heading', { name: '把会话放在中心', exact: true })).toBeVisible();
  await page.screenshot({ path: '../.workstation-agent/frontend-preview.png', fullPage: true });
});
