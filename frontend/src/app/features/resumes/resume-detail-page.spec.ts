import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { Observable, of, throwError } from 'rxjs';

import { ApiError } from '../../core/http/api-error';
import {
  ExtractionItem,
  ExtractionItemUpdate,
  NewExtractionItem,
  ResumeApi,
  ResumeDetail,
} from './resume-api';
import { ResumeDetailPage } from './resume-detail-page';

function makeItem(overrides: Partial<ExtractionItem> = {}): ExtractionItem {
  return {
    id: 'i-1',
    kind: 'skill',
    fields: { name: 'Python' },
    origin: 'explicit',
    evidence: ['Built APIs in Python'],
    ...overrides,
  };
}

function makeDetail(overrides: Partial<ResumeDetail> = {}): ResumeDetail {
  return {
    id: 'r-1',
    filename: 'cv.pdf',
    uploaded_at: '2026-03-10T12:00:00Z',
    status: 'ready',
    failure_code: null,
    failure_message: null,
    items: [],
    ...overrides,
  };
}

class ResumeApiStub {
  get = vi.fn<(id: string) => Observable<ResumeDetail>>(() => of(makeDetail()));
  addItem = vi.fn<(resumeId: string, item: NewExtractionItem) => Observable<ExtractionItem>>();
  updateItem =
    vi.fn<
      (resumeId: string, itemId: string, update: ExtractionItemUpdate) => Observable<ExtractionItem>
    >();
  removeItem = vi.fn<(resumeId: string, itemId: string) => Observable<void>>(() => of(undefined));
}

describe('ResumeDetailPage', () => {
  let fixture: ComponentFixture<ResumeDetailPage>;
  let api: ResumeApiStub;
  let root: HTMLElement;

  const text = (): string => root.textContent ?? '';
  const items = (): HTMLElement[] =>
    Array.from(root.querySelectorAll<HTMLElement>('[data-testid="extraction-item"]'));
  const buttonByText = (label: string, scope: ParentNode = root): HTMLButtonElement => {
    const found = Array.from(scope.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => b.textContent?.trim() === label,
    );
    if (!found) {
      throw new Error(`Button "${label}" not found`);
    }
    return found;
  };
  const inputByLabel = (label: string): HTMLInputElement | HTMLTextAreaElement => {
    const lbl = Array.from(root.querySelectorAll('label')).find(
      (l) => l.textContent?.trim() === label,
    );
    const id = lbl?.getAttribute('for');
    const control = id
      ? root.querySelector<HTMLInputElement | HTMLTextAreaElement>(`#${id}`)
      : null;
    if (!control) {
      throw new Error(`Field "${label}" not found`);
    }
    return control;
  };

  async function settle(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function render(): Promise<void> {
    fixture = TestBed.createComponent(ResumeDetailPage);
    root = fixture.nativeElement as HTMLElement;
    document.body.appendChild(root);
    await settle();
  }

  function type(control: HTMLInputElement | HTMLTextAreaElement, value: string): void {
    control.value = value;
    control.dispatchEvent(new Event('input'));
  }

  beforeEach(async () => {
    api = new ResumeApiStub();
    await TestBed.configureTestingModule({
      imports: [ResumeDetailPage],
      providers: [
        provideRouter([]),
        { provide: ResumeApi, useValue: api },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: convertToParamMap({ id: 'r-1' }) } },
        },
      ],
    }).compileComponents();
  });

  afterEach(() => {
    vi.useRealTimers();
    fixture?.destroy();
    root?.remove();
  });

  it('loads the version from the route id', async () => {
    await render();
    expect(api.get).toHaveBeenCalledWith('r-1');
    expect(text()).toContain('cv.pdf');
  });

  it('shows the "Inferred" label and the cited evidence of an inferred item (CV-07)', async () => {
    api.get.mockReturnValue(
      of(
        makeDetail({
          items: [
            makeItem({
              id: 'i-2',
              fields: { name: 'Docker' },
              origin: 'inferred',
              evidence: ['Deployed services in containers', 'Wrote compose files'],
            }),
          ],
        }),
      ),
    );
    await render();

    expect(items()).toHaveLength(1);
    const item = items()[0];
    expect(item.textContent).toContain('Docker');
    expect(item.textContent).toContain('Inferred');
    const quotes = Array.from(item.querySelectorAll('blockquote')).map((q) =>
      q.textContent?.trim(),
    );
    expect(quotes).toEqual(['Deployed services in containers', 'Wrote compose files']);
  });

  it('groups items into experience, education and skills with their origin labels', async () => {
    api.get.mockReturnValue(
      of(
        makeDetail({
          items: [
            makeItem({
              id: 'e',
              kind: 'experience',
              fields: { title: 'Engineer', organization: 'Acme' },
            }),
            makeItem({ id: 'd', kind: 'education', fields: { degree: 'BSc', institution: 'UFX' } }),
            makeItem({ id: 's', origin: 'user_provided', evidence: [] }),
          ],
        }),
      ),
    );
    await render();

    const section = (title: string): HTMLElement => {
      const heading = Array.from(root.querySelectorAll('h2')).find(
        (h) => h.textContent?.trim() === title,
      );
      const region = heading?.closest('section');
      if (!region) {
        throw new Error(`Section "${title}" not found`);
      }
      return region;
    };
    expect(section('Experience').textContent).toContain('Engineer');
    expect(section('Experience').textContent).toContain('Explicit');
    expect(section('Education').textContent).toContain('BSc');
    expect(section('Skills').textContent).toContain('Provided by you');
    expect(section('Skills').querySelector('blockquote')).toBeNull();
  });

  it('renders untrusted text through interpolation only', async () => {
    api.get.mockReturnValue(
      of(makeDetail({ items: [makeItem({ fields: { name: '<img src=x onerror=alert(1)>' } })] })),
    );
    await render();
    expect(root.querySelector('img')).toBeNull();
    expect(text()).toContain('<img src=x onerror=alert(1)>');
  });

  it('edits an item with updateItem and then shows "Provided by you" (CV-10)', async () => {
    api.get.mockReturnValue(of(makeDetail({ items: [makeItem({ origin: 'inferred' })] })));
    api.updateItem.mockImplementation((_r, itemId, update) =>
      of(
        makeItem({
          id: itemId,
          fields: update.fields,
          origin: 'user_provided',
          evidence: [],
        }),
      ),
    );
    await render();

    buttonByText('Edit', items()[0]).click();
    await settle();
    type(inputByLabel('Name'), 'Python 3');
    await settle();
    buttonByText('Save').click();
    await settle();

    expect(api.updateItem).toHaveBeenCalledWith('r-1', 'i-1', { fields: { name: 'Python 3' } });
    const item = items()[0];
    expect(item.textContent).toContain('Python 3');
    expect(item.textContent).toContain('Provided by you');
    expect(item.textContent).not.toContain('Inferred');
    expect(item.querySelector('blockquote')).toBeNull();
  });

  it('keeps fields the form does not show when editing', async () => {
    api.get.mockReturnValue(
      of(makeDetail({ items: [makeItem({ fields: { name: 'Go', description: 'Backend' } })] })),
    );
    api.updateItem.mockImplementation((_r, itemId, update) =>
      of(makeItem({ id: itemId, fields: update.fields, origin: 'user_provided', evidence: [] })),
    );
    await render();

    buttonByText('Edit', items()[0]).click();
    await settle();
    buttonByText('Save').click();
    await settle();

    expect(api.updateItem).toHaveBeenCalledWith('r-1', 'i-1', {
      fields: { name: 'Go', description: 'Backend' },
    });
  });

  it('adds an item with addItem', async () => {
    api.addItem.mockImplementation((_r, item) =>
      of(makeItem({ id: 'new', ...item, origin: 'user_provided', evidence: [] })),
    );
    await render();

    buttonByText('Add skill').click();
    await settle();
    type(inputByLabel('Name'), 'Rust');
    await settle();
    buttonByText('Save').click();
    await settle();

    expect(api.addItem).toHaveBeenCalledWith('r-1', { kind: 'skill', fields: { name: 'Rust' } });
    expect(items()).toHaveLength(1);
    expect(items()[0].textContent).toContain('Rust');
    expect(items()[0].textContent).toContain('Provided by you');
  });

  it('removes an item after confirmation', async () => {
    api.get.mockReturnValue(of(makeDetail({ items: [makeItem()] })));
    await render();

    buttonByText('Remove', items()[0]).click();
    await settle();
    const dialog = root.querySelector('dialog');
    expect(dialog).not.toBeNull();
    buttonByText('Remove', dialog as HTMLElement).click();
    await settle();

    expect(api.removeItem).toHaveBeenCalledWith('r-1', 'i-1');
    expect(items()).toHaveLength(0);
  });

  it('shows the catalog text when an edit fails', async () => {
    api.get.mockReturnValue(of(makeDetail({ items: [makeItem()] })));
    api.updateItem.mockReturnValue(
      throwError(() => ({ code: 'VALIDATION_ERROR', message: 'raw' }) satisfies ApiError),
    );
    await render();

    buttonByText('Edit', items()[0]).click();
    await settle();
    buttonByText('Save').click();
    await settle();

    expect(root.querySelector('[role="alert"]')?.textContent).toContain(
      'Please check the highlighted fields.',
    );
    expect(text()).not.toContain('raw');
  });

  it('does not show the extraction of a failed version', async () => {
    api.get.mockReturnValue(
      of(
        makeDetail({
          status: 'failed',
          failure_code: 'NOT_ENGLISH',
          failure_message: 'raw failure',
          items: [makeItem({ fields: { name: 'Hidden skill' } })],
        }),
      ),
    );
    await render();

    expect(items()).toHaveLength(0);
    expect(text()).not.toContain('Hidden skill');
    expect(text()).toContain('Only resumes in English are supported at the moment.');
    expect(text()).not.toContain('raw failure');
    expect(root.querySelectorAll('button')).toHaveLength(0);
  });

  it('does not allow editing while the version is processing', async () => {
    vi.useFakeTimers();
    api.get.mockReturnValue(of(makeDetail({ status: 'processing', items: null })));
    await render();

    expect(text()).toContain('Processing');
    expect(root.querySelectorAll('button')).toHaveLength(0);

    api.get.mockReturnValue(of(makeDetail({ items: [makeItem()] })));
    await vi.advanceTimersByTimeAsync(3000);
    await settle();
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(items()).toHaveLength(1);
  });

  it('treats an unknown or foreign id as not found', async () => {
    api.get.mockReturnValue(
      throwError(() => ({ code: 'RESOURCE_NOT_FOUND', message: 'x' }) satisfies ApiError),
    );
    await render();
    expect(root.querySelector('[role="alert"]')?.textContent).toContain('Not found.');
  });
});
