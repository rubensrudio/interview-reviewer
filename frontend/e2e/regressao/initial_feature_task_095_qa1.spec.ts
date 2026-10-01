import { Route, expect, test } from '@playwright/test';

const ME = {
  id: 'u-1',
  email: 'alice@example.com',
  has_password: true,
  google_linked: false,
  terms_accepted: true,
};

const DELETED_MESSAGE = 'Your account and data were deleted. Backup copies expire within 30 days.';
const XSRF_TOKEN = 'xsrf-test-token';

test('DATA-06 — Confirmação de exclusão de conta nunca fica visível (navega para /login no mesmo tick)', async ({
  page,
  context,
  baseURL,
}) => {
  let deleted = false;

  await context.addCookies([{ name: 'XSRF-TOKEN', value: XSRF_TOKEN, url: baseURL ?? 'http://localhost:4200' }]);

  await page.route('**/api/**', (route: Route) =>
    route.fulfill({
      status: 404,
      json: { error: { code: 'RESOURCE_NOT_FOUND', message: 'Not found.' } },
    }),
  );
  await page.route('**/api/auth/me', (route: Route) =>
    deleted
      ? route.fulfill({
          status: 401,
          json: { error: { code: 'AUTH_REQUIRED', message: 'Authentication required.' } },
        })
      : route.fulfill({ json: ME }),
  );
  await page.route('**/api/account', async (route: Route) => {
    if (route.request().method() !== 'DELETE') {
      await route.fallback();
      return;
    }
    deleted = true;
    await route.fulfill({ json: { message: DELETED_MESSAGE } });
  });

  await page.goto('/account');
  await expect(page.getByText(ME.email)).toBeVisible();

  await page.getByRole('button', { name: 'Delete account' }).click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog).toBeVisible();

  const deleteRequest = page.waitForRequest(
    (request) => request.method() === 'DELETE' && new URL(request.url()).pathname === '/api/account',
  );
  await dialog.getByRole('button', { name: 'Delete account' }).click();
  const request = await deleteRequest;

  // Contract (plan 8.1): DELETE /api/account, no body, CSRF header.
  expect(request.postData()).toBeNull();
  expect(request.headers()['x-xsrf-token']).toBe(XSRF_TOKEN);

  // The confirmation must stay on screen until the user moves on.
  const status = page.getByRole('status');
  await expect(status).toHaveText(DELETED_MESSAGE);
  await expect(status).toBeFocused();
  await expect(page).toHaveURL(/\/account$/);

  await page.getByRole('link', { name: 'Go to sign in' }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
});
