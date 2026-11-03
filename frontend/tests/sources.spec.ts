import { expect, test } from '@playwright/test';
import type { AnswerSource, Conversation, Message } from '../src/api/contracts';

test('回答引用展开已保存原文，并链接到工作台当前笔记', async ({ page }, testInfo) => {
  await page.goto('/?demo=1');
  await page.getByRole('button', { name: '前端开发指南', exact: true }).first().click();
  const panel = page.getByRole('region', { name: '来源详情' });
  await expect(panel).toBeVisible();
  await expect(panel).toContainText('会话保存持续的对话，运行对应一次任务执行。');
  await expect(panel).toContainText('版本 7');
  const link = panel.getByRole('link', { name: '打开工作台原笔记' });
  await expect(link).toHaveAttribute('href', 'https://workstation.example.com/w/11/notes/42/edit');
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  await page.screenshot({ path: testInfo.outputPath('sources-desktop.png') });
  await panel.getByRole('button', { name: '关闭来源详情' }).click();
  await expect(panel).toHaveCount(0);
});

test('来源列表支持窄屏和键盘展开，页面无横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?demo=1');
  await page.getByRole('button', { name: '收起运行详情', exact: true }).click();
  const source = page.getByRole('button', { name: '1 前端开发指南' });
  await source.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('region', { name: '来源详情' })).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});

test('刷新后重新加载来源，伪造引用不能打开详情，原文不执行 HTML', async ({ page }) => {
  const date = new Date().toISOString();
  const chat: Conversation = { id: 'session_sources', title: '来源验证', revision: 2, created_at: date, updated_at: date };
  const source: AnswerSource = {
    id: 'src_1234567890abcdef12345678', document_id: 'doc', checksum: 'a'.repeat(64),
    source_uri: 'workstation://main/workspaces/11/notes/42', title: '可信片段', citation: 'note:1-2',
    content: '<script>window.sourceExecuted=true</script>\n这是保存的原始内容。',
    line_start: 1, line_end: 2, source_version: '7', original_url: null,
  };
  const message: Message = { id: 'answer', session_id: chat.id, position: 2, role: 'assistant',
    content: `[可信片段](#source-${source.id}) 和 [伪造来源](#source-src_000000000000000000000000)`,
    run_id: null, created_at: date, updated_at: date, sources: [source] };
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith('/api/')) { await route.continue(); return; }
    const body = path === '/api/sessions' ? { items: [chat], next_offset: null }
      : path.endsWith('/messages') ? { items: [message], next_offset: null }
      : path.endsWith('/approvals') ? {} : path === '/api/runs' ? { items: [], next_offset: null } : chat;
    await route.fulfill({ json: body });
  });
  await page.goto('/');
  for (let attempt = 0; attempt < 2; attempt++) {
    if (attempt) await page.reload();
    await page.getByRole('button', { name: '连接本机 Agent', exact: true }).click();
    await page.getByLabel('访问令牌', { exact: true }).fill('source_test_' + 'a'.repeat(32));
    await page.getByRole('button', { name: '连接服务', exact: true }).last().click();
    await page.getByRole('button', { name: '可信片段', exact: true }).click();
    const panel = page.getByRole('region', { name: '来源详情' });
    await expect(panel).toContainText(source.content);
    await expect(page.getByRole('button', { name: '伪造来源' })).toHaveCount(0);
    await expect(page.locator('a[href^="#source-"]')).toHaveCount(0);
    await expect(panel.getByRole('link')).toHaveCount(0);
    expect(await page.evaluate(() => Reflect.get(window, 'sourceExecuted'))).toBeUndefined();
  }
});
