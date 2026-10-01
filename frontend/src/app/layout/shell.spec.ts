import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Observable, Subject, of, throwError } from 'rxjs';

import { AuthApi } from '../core/auth/auth-api';
import { ApiError } from '../core/http/api-error';
import { Shell } from './shell';

@Component({ template: '' })
class BlankPage {}

class AuthApiStub {
  logout = vi.fn<() => Observable<void>>(() => of(undefined));
}

describe('Shell', () => {
  let fixture: ComponentFixture<Shell>;
  let root: HTMLElement;
  let api: AuthApiStub;
  let router: Router;

  const signOutButton = (): HTMLButtonElement => {
    const found = Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => b.textContent?.trim() === 'Sign out',
    );
    if (!found) {
      throw new Error('Sign out button not found');
    }
    return found;
  };

  beforeEach(async () => {
    api = new AuthApiStub();
    await TestBed.configureTestingModule({
      imports: [Shell],
      providers: [
        provideRouter([{ path: '**', component: BlankPage }]),
        { provide: AuthApi, useValue: api },
      ],
    }).compileComponents();
    router = TestBed.inject(Router);
    fixture = TestBed.createComponent(Shell);
    root = fixture.nativeElement as HTMLElement;
    fixture.detectChanges();
    await fixture.whenStable();
  });

  it('renders a main navigation landmark with the four section links', () => {
    const nav = root.querySelector('nav[aria-label="Main"]');
    expect(nav).not.toBeNull();
    const links = Array.from(nav?.querySelectorAll<HTMLAnchorElement>('a') ?? []);
    expect(links.map((a) => a.textContent?.trim())).toEqual([
      'Resumes',
      'New interview',
      'History',
      'Account',
    ]);
    expect(links.map((a) => a.getAttribute('href'))).toEqual([
      '/resumes',
      '/sessions/new',
      '/history',
      '/account',
    ]);
  });

  it('exposes a main region for the routed page', () => {
    const main = root.querySelector('main');
    expect(main).not.toBeNull();
    expect(main?.querySelector('router-outlet')).not.toBeNull();
  });

  it('renders footer links to the privacy policy and the terms', () => {
    const links = Array.from(root.querySelectorAll<HTMLAnchorElement>('footer a'));
    expect(links.map((a) => [a.textContent?.trim(), a.getAttribute('href')])).toEqual([
      ['Privacy', '/privacy'],
      ['Terms', '/terms'],
    ]);
  });

  it('signs out through AuthApi.logout and navigates to /login', async () => {
    const navigate = vi.spyOn(router, 'navigateByUrl');

    signOutButton().click();
    await fixture.whenStable();

    expect(api.logout).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith('/login');
  });

  it('disables the button while signing out to avoid duplicate requests', () => {
    const pending = new Subject<void>();
    api.logout.mockReturnValue(pending.asObservable());

    signOutButton().click();
    fixture.detectChanges();

    expect(signOutButton().disabled).toBe(true);
    expect(signOutButton().getAttribute('aria-busy')).toBe('true');
    signOutButton().click();
    expect(api.logout).toHaveBeenCalledTimes(1);
  });

  it('still goes to /login when the session was already gone (401)', async () => {
    const error: ApiError = { code: 'AUTH_REQUIRED', message: 'Authentication required' };
    api.logout.mockReturnValue(throwError(() => error));
    const navigate = vi.spyOn(router, 'navigateByUrl');

    signOutButton().click();
    await fixture.whenStable();

    expect(navigate).toHaveBeenCalledWith('/login');
  });

  it('stays on the page and announces an error when sign out fails', async () => {
    const error: ApiError = { code: 'NETWORK_ERROR', message: 'Network error' };
    api.logout.mockReturnValue(throwError(() => error));
    const navigate = vi.spyOn(router, 'navigateByUrl');

    signOutButton().click();
    fixture.detectChanges();
    await fixture.whenStable();

    expect(navigate).not.toHaveBeenCalledWith('/login');
    const alert = root.querySelector('[role="alert"]');
    expect(alert?.textContent?.trim()).toBe('Could not sign out. Please try again.');
    expect(signOutButton().disabled).toBe(false);
  });
});
