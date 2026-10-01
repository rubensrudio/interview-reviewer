import { DatePipe, NgTemplateOutlet } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  OnInit,
  afterNextRender,
  computed,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Subscription, timer } from 'rxjs';

import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';
import { ConfirmDialog } from '../../shared/confirm-dialog';
import {
  ExtractionItem,
  ExtractionKind,
  ExtractionOrigin,
  ResumeApi,
  ResumeDetail,
  ResumeStatus,
} from './resume-api';

/** Interval between reloads while the version is `received` or `processing` (same as the list). */
const POLL_INTERVAL_MS = 3000;

const GENERIC_ERROR = 'Something went wrong. Please try again.';
const NOT_FOUND_MESSAGE = 'Not found.';
const NOT_READY_MESSAGE = 'This resume is not ready yet.';

/** Section 9 / catalog 8.3 texts for request errors, keyed by `code`. */
const ERRORS: Readonly<Record<string, string>> = {
  VALIDATION_ERROR: 'Please check the highlighted fields.',
  CSRF_FAILED: 'Your session expired. Please reload the page.',
  RESOURCE_NOT_FOUND: NOT_FOUND_MESSAGE,
  RESUME_NOT_READY: NOT_READY_MESSAGE,
  [NETWORK_ERROR]: 'Unable to reach the server.',
};

/** Section 9 texts for asynchronous processing failures, keyed by `failure_code`. */
const NO_TEXT_MESSAGE =
  "We couldn't read text from this PDF. Scanned documents are not supported yet — please upload a text-based PDF.";
const PROCESSING_FAILED_MESSAGE =
  "We couldn't process this resume. Please try uploading it again later.";
const FAILURES: Readonly<Record<string, string>> = {
  NO_TEXT: NO_TEXT_MESSAGE,
  OCR_NO_TEXT: NO_TEXT_MESSAGE,
  NOT_ENGLISH: 'Only resumes in English are supported at the moment.',
};

const STATUS_LABELS: Readonly<Record<ResumeStatus, string>> = {
  received: 'Received',
  processing: 'Processing',
  ready: 'Ready',
  failed: 'Failed',
};

const ORIGIN_LABELS: Readonly<Record<ExtractionOrigin, string>> = {
  explicit: 'Explicit',
  inferred: 'Inferred',
  user_provided: 'Provided by you',
};

export const REMOVE_ITEM_TITLE = 'Remove this item?';
export const REMOVE_ITEM_MESSAGE = "It will be removed from this resume's extraction.";

/** Display order and labels of the field keys allowed by the backend (plan 7.3). */
const FIELD_LABELS: Readonly<Record<string, string>> = {
  title: 'Title',
  organization: 'Organization',
  degree: 'Degree',
  institution: 'Institution',
  name: 'Name',
  start: 'Start',
  end: 'End',
  description: 'Description',
};
const FIELD_ORDER = Object.keys(FIELD_LABELS);
const MULTILINE_FIELDS: ReadonlySet<string> = new Set(['description']);

interface KindSection {
  kind: ExtractionKind;
  title: string;
  addLabel: string;
  emptyText: string;
  /** Form fields offered for this kind; the first one is the item's heading. */
  fields: readonly string[];
}

const SECTIONS: readonly KindSection[] = [
  {
    kind: 'experience',
    title: 'Experience',
    addLabel: 'Add experience',
    emptyText: 'No experience listed.',
    fields: ['title', 'organization', 'start', 'end', 'description'],
  },
  {
    kind: 'education',
    title: 'Education',
    addLabel: 'Add education',
    emptyText: 'No education listed.',
    fields: ['degree', 'institution', 'start', 'end', 'description'],
  },
  {
    kind: 'skill',
    title: 'Skills',
    addLabel: 'Add skill',
    emptyText: 'No skills listed.',
    fields: ['name', 'description'],
  },
];

const SECTION_BY_KIND: Readonly<Record<ExtractionKind, KindSection>> = {
  experience: SECTIONS[0],
  education: SECTIONS[1],
  skill: SECTIONS[2],
};

/** The item form currently open: adding to a section or editing an existing item. */
type Editor = { mode: 'add'; kind: ExtractionKind } | { mode: 'edit'; itemId: string };

function isPending(status: ResumeStatus): boolean {
  return status === 'received' || status === 'processing';
}

function orderedKeys(keys: Iterable<string>): string[] {
  const present = new Set(keys);
  const known = FIELD_ORDER.filter((key) => present.has(key));
  const unknown = [...present].filter((key) => !(key in FIELD_LABELS));
  return [...known, ...unknown];
}

/**
 * Resume extraction review page (CV-07, CV-10).
 *
 * Shows experience, education and skills of a `ready` version with their origin label and cited
 * evidence, and lets the candidate add, edit and remove items. The backend decides validity and
 * origin: an added or edited item is shown exactly as the API returns it. Versions that are not
 * `ready` never show the extraction nor allow edits; pending ones are polled until they finish.
 */
@Component({
  selector: 'app-resume-detail-page',
  imports: [DatePipe, NgTemplateOutlet, RouterLink, ConfirmDialog],
  templateUrl: './resume-detail-page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    :host {
      display: block;
      max-width: 48rem;
      margin: 0 auto;
      padding: 2rem 1rem;
      color: #1a1a1a;
    }
    h1 {
      margin: 0 0 0.5rem;
      font-size: 1.75rem;
      overflow-wrap: anywhere;
    }
    h2 {
      margin: 0;
      font-size: 1.25rem;
    }
    h3 {
      margin: 0;
      font-size: 1.0625rem;
      overflow-wrap: anywhere;
    }
    p {
      margin: 0;
    }
    a {
      color: #1d4ed8;
    }
    .back {
      display: inline-block;
      margin-bottom: 1rem;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem 1rem;
      margin-bottom: 2rem;
      color: #4b5563;
    }
    .status {
      font-weight: 600;
    }
    section {
      display: flex;
      flex-direction: column;
      gap: 1rem;
      margin-bottom: 2rem;
    }
    .section-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }
    ul.items {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    li.item {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      padding: 1rem;
      border: 1px solid #c4c4c4;
      border-radius: 0.5rem;
    }
    .item-head {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      justify-content: space-between;
      gap: 0.5rem;
    }
    .origin {
      padding: 0.125rem 0.5rem;
      border-radius: 999px;
      font-size: 0.875rem;
      font-weight: 600;
    }
    .origin-explicit {
      color: #166534;
      background: #f0fdf4;
      border: 1px solid #166534;
    }
    .origin-inferred {
      color: #92400e;
      background: #fffbeb;
      border: 1px solid #92400e;
    }
    .origin-user_provided {
      color: #1e3a8a;
      background: #eff6ff;
      border: 1px solid #1e3a8a;
    }
    dl {
      display: grid;
      grid-template-columns: max-content 1fr;
      gap: 0.25rem 0.75rem;
      margin: 0;
    }
    dt {
      font-weight: 600;
    }
    dd {
      margin: 0;
      overflow-wrap: anywhere;
      white-space: pre-line;
    }
    .evidence {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }
    .evidence-title {
      font-weight: 600;
    }
    blockquote {
      margin: 0;
      padding: 0.25rem 0.75rem;
      border-left: 3px solid #6b7280;
      color: #374151;
      overflow-wrap: anywhere;
      white-space: pre-line;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
    }
    form {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      padding: 1rem;
      border: 1px solid #4b5563;
      border-radius: 0.5rem;
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
    }
    label {
      font-weight: 600;
    }
    input,
    textarea {
      min-height: 2.75rem;
      padding: 0.5rem 0.75rem;
      border: 1px solid #6b7280;
      border-radius: 0.375rem;
      font: inherit;
    }
    textarea {
      min-height: 6rem;
      resize: vertical;
    }
    .form-error {
      padding: 0.75rem 1rem;
      border: 1px solid #b91c1c;
      border-radius: 0.375rem;
      color: #7f1d1d;
      background: #fef2f2;
    }
    .failure {
      color: #7f1d1d;
    }
    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
    }
    button {
      min-height: 2.75rem;
      padding: 0.5rem 1rem;
      border-radius: 0.375rem;
      font: inherit;
      cursor: pointer;
    }
    .primary {
      border: 1px solid #1d4ed8;
      color: #ffffff;
      background: #1d4ed8;
    }
    .secondary {
      border: 1px solid #4b5563;
      color: #1a1a1a;
      background: #ffffff;
    }
    .danger {
      border: 1px solid #b91c1c;
      color: #b91c1c;
      background: #ffffff;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.6;
    }
    button[aria-busy='true'] {
      cursor: progress;
    }
    button:focus-visible,
    input:focus-visible,
    textarea:focus-visible,
    a:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
  `,
})
export class ResumeDetailPage implements OnInit {
  private readonly api = inject(ResumeApi);
  private readonly destroyRef = inject(DestroyRef);
  private readonly injector = inject(Injector);
  private readonly resumeId = inject(ActivatedRoute).snapshot.paramMap.get('id') ?? '';
  private readonly formRef = viewChild<ElementRef<HTMLFormElement>>('itemForm');

  protected readonly resume = signal<ResumeDetail | null>(null);
  protected readonly loading = signal(true);
  protected readonly loadError = signal<string | null>(null);
  protected readonly editor = signal<Editor | null>(null);
  protected readonly draft = signal<Readonly<Partial<Record<string, string>>>>({});
  protected readonly draftKeys = signal<readonly string[]>([]);
  protected readonly saving = signal(false);
  protected readonly formError = signal<string | null>(null);
  protected readonly actionError = signal<string | null>(null);
  protected readonly pendingRemove = signal<ExtractionItem | null>(null);
  protected readonly removingId = signal<string | null>(null);

  protected readonly sections = SECTIONS;
  protected readonly statusLabels = STATUS_LABELS;
  protected readonly originLabels = ORIGIN_LABELS;
  protected readonly fieldLabels = FIELD_LABELS;
  protected readonly multilineFields = MULTILINE_FIELDS;
  protected readonly removeTitle = REMOVE_ITEM_TITLE;
  protected readonly removeMessage = REMOVE_ITEM_MESSAGE;

  /** Items are shown and editable only for `ready` versions (CV-07; "não editar fora de ready"). */
  protected readonly isReady = computed(() => this.resume()?.status === 'ready');
  protected readonly itemsByKind = computed(() => {
    const grouped: Record<ExtractionKind, ExtractionItem[]> = {
      experience: [],
      education: [],
      skill: [],
    };
    const current = this.resume();
    if (current?.status === 'ready') {
      for (const item of current.items ?? []) {
        grouped[item.kind]?.push(item);
      }
    }
    return grouped;
  });
  protected readonly failureMessage = computed(() => {
    const current = this.resume();
    if (current?.status !== 'failed') {
      return null;
    }
    return (current.failure_code && FAILURES[current.failure_code]) || PROCESSING_FAILED_MESSAGE;
  });
  protected readonly pending = computed(() => {
    const current = this.resume();
    return current !== null && isPending(current.status);
  });

  private loadSub: Subscription | null = null;
  private pollSub: Subscription | null = null;

  ngOnInit(): void {
    this.destroyRef.onDestroy(() => {
      this.loadSub?.unsubscribe();
      this.pollSub?.unsubscribe();
    });
    this.load();
  }

  protected retry(): void {
    this.loading.set(true);
    this.loadError.set(null);
    this.load();
  }

  protected heading(item: ExtractionItem): string {
    const primary = SECTION_BY_KIND[item.kind]?.fields[0];
    const value = primary ? item.fields[primary] : undefined;
    return value || Object.values(item.fields).find((v) => v) || '';
  }

  /** Field keys shown under the heading: every present key except the heading one. */
  protected detailKeys(item: ExtractionItem): string[] {
    const primary = SECTION_BY_KIND[item.kind]?.fields[0];
    const keys = orderedKeys(Object.keys(item.fields)).filter((key) => item.fields[key]);
    const headingKey = primary && item.fields[primary] ? primary : keys[0];
    return keys.filter((key) => key !== headingKey);
  }

  protected labelFor(key: string): string {
    return FIELD_LABELS[key] ?? key;
  }

  protected isAdding(kind: ExtractionKind): boolean {
    const current = this.editor();
    return current?.mode === 'add' && current.kind === kind;
  }

  protected isEditing(item: ExtractionItem): boolean {
    const current = this.editor();
    return current?.mode === 'edit' && current.itemId === item.id;
  }

  protected startAdd(kind: ExtractionKind): void {
    this.openEditor({ mode: 'add', kind }, SECTION_BY_KIND[kind].fields, {});
  }

  protected startEdit(item: ExtractionItem): void {
    // Union of the kind's fields and the item's own keys, so saving never drops a field.
    const keys = orderedKeys([...SECTION_BY_KIND[item.kind].fields, ...Object.keys(item.fields)]);
    this.openEditor({ mode: 'edit', itemId: item.id }, keys, { ...item.fields });
  }

  protected cancelEdit(): void {
    this.editor.set(null);
    this.formError.set(null);
  }

  protected setField(key: string, event: Event): void {
    const value = (event.target as HTMLInputElement | HTMLTextAreaElement).value;
    this.draft.update((current) => ({ ...current, [key]: value }));
  }

  protected save(event: Event): void {
    event.preventDefault();
    const current = this.editor();
    if (!current || this.saving() || !this.isReady()) {
      return;
    }
    const fields = this.draftFields();
    this.saving.set(true);
    this.formError.set(null);

    const request =
      current.mode === 'add'
        ? this.api.addItem(this.resumeId, { kind: current.kind, fields })
        : this.api.updateItem(this.resumeId, current.itemId, { fields });

    request.pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (saved) => {
        this.saving.set(false);
        this.editor.set(null);
        this.replaceItem(saved, current.mode === 'edit' ? current.itemId : null);
      },
      error: (err: ApiError) => {
        this.saving.set(false);
        this.formError.set(ERRORS[err.code] ?? GENERIC_ERROR);
        this.reloadIfNotReady(err);
      },
    });
  }

  protected askRemove(item: ExtractionItem): void {
    this.actionError.set(null);
    this.pendingRemove.set(item);
  }

  protected cancelRemove(): void {
    this.pendingRemove.set(null);
  }

  protected confirmRemove(): void {
    const target = this.pendingRemove();
    this.pendingRemove.set(null);
    if (!target || !this.isReady()) {
      return;
    }
    this.removingId.set(target.id);
    this.api
      .removeItem(this.resumeId, target.id)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => {
          this.removingId.set(null);
          if (this.isEditing(target)) {
            this.editor.set(null);
          }
          this.updateItems((list) => list.filter((item) => item.id !== target.id));
        },
        error: (err: ApiError) => {
          this.removingId.set(null);
          this.actionError.set(ERRORS[err.code] ?? GENERIC_ERROR);
          this.reloadIfNotReady(err);
        },
      });
  }

  private openEditor(
    editor: Editor,
    keys: readonly string[],
    values: Readonly<Record<string, string>>,
  ): void {
    this.formError.set(null);
    this.actionError.set(null);
    this.draftKeys.set(keys);
    this.draft.set(values);
    this.editor.set(editor);
    afterNextRender(
      () => this.formRef()?.nativeElement.querySelector<HTMLElement>('input, textarea')?.focus(),
      { injector: this.injector },
    );
  }

  /** Non-blank form values; the backend validates and normalizes them again. */
  private draftFields(): Record<string, string> {
    const values = this.draft();
    const fields: Record<string, string> = {};
    for (const key of this.draftKeys()) {
      const value = values[key];
      if (value !== undefined && value.trim() !== '') {
        fields[key] = value;
      }
    }
    return fields;
  }

  private replaceItem(saved: ExtractionItem, previousId: string | null): void {
    this.updateItems((list) =>
      previousId === null
        ? [...list, saved]
        : list.map((item) => (item.id === previousId ? saved : item)),
    );
  }

  private updateItems(change: (list: ExtractionItem[]) => ExtractionItem[]): void {
    this.resume.update((current) =>
      current ? { ...current, items: change(current.items ?? []) } : current,
    );
  }

  private reloadIfNotReady(err: ApiError): void {
    if (err.code === 'RESUME_NOT_READY') {
      this.editor.set(null);
      this.load();
    }
  }

  private load(): void {
    this.loadSub?.unsubscribe();
    this.loadSub = this.api.get(this.resumeId).subscribe({
      next: (detail) => {
        this.loading.set(false);
        this.loadError.set(null);
        this.resume.set(detail);
        this.schedulePoll();
      },
      error: (err: ApiError) => {
        this.loading.set(false);
        // A failed background refresh keeps the current state and tries again later.
        if (this.pending() && err.code !== 'RESOURCE_NOT_FOUND') {
          this.schedulePoll();
          return;
        }
        this.resume.set(null);
        this.loadError.set(ERRORS[err.code] ?? GENERIC_ERROR);
      },
    });
  }

  /** Reloads after `POLL_INTERVAL_MS` while the version is still being processed. */
  private schedulePoll(): void {
    if ((this.pollSub && !this.pollSub.closed) || !this.pending()) {
      return;
    }
    this.pollSub = timer(POLL_INTERVAL_MS).subscribe(() => {
      this.pollSub = null;
      this.load();
    });
  }
}
