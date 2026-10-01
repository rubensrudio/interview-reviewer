import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { routes } from '../../app.routes';
import { PrivacyPage } from './privacy-page';
import { TermsPage } from './terms-page';

describe('PrivacyPage', () => {
  let fixture: ComponentFixture<PrivacyPage>;
  let root: HTMLElement;

  const text = (): string => root.textContent?.replace(/\s+/g, ' ') ?? '';

  beforeEach(async () => {
    await TestBed.configureTestingModule({ providers: [provideRouter([])] }).compileComponents();
    fixture = TestBed.createComponent(PrivacyPage);
    root = fixture.nativeElement as HTMLElement;
    fixture.detectChanges();
    await fixture.whenStable();
  });

  it('states the retention period, how to delete data and that data is not used to train', () => {
    expect(text()).toContain('30 days');
    expect(text()).toContain('delete');
    expect(text()).toContain('not used to train');
  });

  it('covers purpose, inactive-session expiry, backups and model validation', () => {
    expect(text()).toContain('Purpose');
    expect(text()).toContain('expire after 30 days without activity');
    expect(text()).toContain('Backups');
    expect(text()).toContain('validate');
  });

  it('shows the current policy version', () => {
    expect(text()).toContain('privacy-2026-09');
  });

  it('has a single level-one heading and labelled sections', () => {
    expect(root.querySelectorAll('h1')).toHaveLength(1);
    const sections = Array.from(root.querySelectorAll('section[aria-labelledby]'));
    expect(sections.length).toBeGreaterThan(0);
    for (const section of sections) {
      const id = section.getAttribute('aria-labelledby') ?? '';
      expect(root.querySelector(`#${id}`)).not.toBeNull();
    }
  });
});

describe('TermsPage', () => {
  it('shows a heading, the current terms version and a link to the privacy policy', async () => {
    await TestBed.configureTestingModule({ providers: [provideRouter([])] }).compileComponents();
    const fixture = TestBed.createComponent(TermsPage);
    const root = fixture.nativeElement as HTMLElement;
    fixture.detectChanges();
    await fixture.whenStable();

    expect(root.querySelectorAll('h1')).toHaveLength(1);
    expect(root.textContent).toContain('terms-2026-09');
    expect(root.querySelector('a[href="/privacy"]')).not.toBeNull();
  });
});

describe('legal routes', () => {
  it.each(['privacy', 'terms'])('/%s is a public top-level route without guards', (path) => {
    const route = routes.find((r) => r.path === path);
    expect(route).toBeDefined();
    expect(route?.canActivate).toBeUndefined();
    expect(route?.canMatch).toBeUndefined();
  });
});
