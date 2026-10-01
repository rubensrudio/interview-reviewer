import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  OnDestroy,
  input,
  output,
  viewChild,
} from '@angular/core';

let nextDialogId = 0;

/**
 * Shared modal confirmation dialog (CT-59).
 *
 * Opens as soon as it is rendered; the consumer controls its lifetime with
 * `@if` and removes it after handling `confirmed` or `cancelled`.
 * All visible copy comes from inputs; only the generic cancel label has a default.
 */
@Component({
  selector: 'app-confirm-dialog',
  templateUrl: './confirm-dialog.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    dialog {
      max-width: min(28rem, calc(100vw - 2rem));
      padding: 1.5rem;
      border: 1px solid #c4c4c4;
      border-radius: 0.5rem;
      color: #1a1a1a;
      background: #ffffff;
    }
    dialog::backdrop {
      background: rgb(0 0 0 / 0.5);
    }
    h2 {
      margin: 0 0 0.75rem;
      font-size: 1.25rem;
    }
    p {
      margin: 0 0 1.5rem;
      line-height: 1.5;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 0.75rem;
    }
    button {
      min-height: 2.75rem;
      padding: 0.5rem 1rem;
      border-radius: 0.375rem;
      font: inherit;
      cursor: pointer;
    }
    button:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
    .cancel {
      border: 1px solid #4b5563;
      color: #1a1a1a;
      background: #ffffff;
    }
    .confirm {
      border: 1px solid #b91c1c;
      color: #ffffff;
      background: #b91c1c;
    }
  `,
})
export class ConfirmDialog implements AfterViewInit, OnDestroy {
  readonly title = input.required<string>();
  readonly message = input.required<string>();
  readonly confirmLabel = input.required<string>();
  readonly cancelLabel = input<string>('Cancel');

  readonly confirmed = output<void>();
  readonly cancelled = output<void>();

  protected readonly titleId = `confirm-dialog-title-${nextDialogId}`;
  protected readonly messageId = `confirm-dialog-message-${nextDialogId++}`;

  private readonly dialogRef = viewChild.required<ElementRef<HTMLDialogElement>>('dialog');
  private readonly cancelButtonRef =
    viewChild.required<ElementRef<HTMLButtonElement>>('cancelButton');
  private previouslyFocused: HTMLElement | null = null;

  ngAfterViewInit(): void {
    const active = document.activeElement;
    this.previouslyFocused = active instanceof HTMLElement ? active : null;

    const dialog = this.dialogRef().nativeElement;
    if (!dialog.open) {
      if (typeof dialog.showModal === 'function') {
        dialog.showModal();
      } else {
        dialog.setAttribute('open', '');
      }
    }
    this.cancelButtonRef().nativeElement.focus();
  }

  ngOnDestroy(): void {
    const dialog = this.dialogRef().nativeElement;
    if (typeof dialog.close === 'function') {
      dialog.close();
    } else {
      dialog.removeAttribute('open');
    }
    if (this.previouslyFocused?.isConnected) {
      this.previouslyFocused.focus();
    }
  }

  protected confirm(): void {
    this.confirmed.emit();
  }

  protected cancel(): void {
    this.cancelled.emit();
  }

  /** Native `cancel` (Escape / close request): keep the dialog open and let the consumer decide. */
  protected onNativeCancel(event: Event): void {
    event.preventDefault();
    this.cancel();
  }

  protected onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      // Preventing the keydown stops the browser from also firing `cancel`.
      event.preventDefault();
      this.cancel();
      return;
    }
    if (event.key === 'Tab') {
      this.trapFocus(event);
    }
  }

  private trapFocus(event: KeyboardEvent): void {
    const focusables = Array.from(
      this.dialogRef().nativeElement.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    );
    if (focusables.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement;

    if (event.shiftKey && (active === first || !focusables.includes(active as HTMLElement))) {
      event.preventDefault();
      last.focus();
    } else if (
      !event.shiftKey &&
      (active === last || !focusables.includes(active as HTMLElement))
    ) {
      event.preventDefault();
      first.focus();
    }
  }
}
