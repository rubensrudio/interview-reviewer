import { Component, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ConfirmDialog } from './confirm-dialog';

@Component({
  imports: [ConfirmDialog],
  template: `
    <button type="button" id="opener">Open</button>
    @if (open()) {
      <app-confirm-dialog
        title="Delete resume?"
        message="The file and its extraction will be removed."
        confirmLabel="Delete"
        (confirmed)="onConfirmed()"
        (cancelled)="onCancelled()"
      />
    }
  `,
})
class HostComponent {
  readonly open = signal(true);
  confirmedCount = 0;
  cancelledCount = 0;

  onConfirmed(): void {
    this.confirmedCount++;
  }

  onCancelled(): void {
    this.cancelledCount++;
  }
}

describe('ConfirmDialog', () => {
  let fixture: ComponentFixture<HostComponent>;
  let host: HostComponent;
  let root: HTMLElement;

  const dialog = (): HTMLDialogElement => root.querySelector('dialog') as HTMLDialogElement;
  const button = (name: string): HTMLButtonElement => {
    const found = Array.from(root.querySelectorAll<HTMLButtonElement>('dialog button')).find(
      (b) => b.textContent?.trim() === name,
    );
    if (!found) {
      throw new Error(`Button "${name}" not found`);
    }
    return found;
  };
  const press = (target: HTMLElement, key: string, shiftKey = false): KeyboardEvent => {
    const event = new KeyboardEvent('keydown', { key, shiftKey, bubbles: true, cancelable: true });
    target.dispatchEvent(event);
    return event;
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [HostComponent] }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    host = fixture.componentInstance;
    root = fixture.nativeElement as HTMLElement;
    document.body.appendChild(root);
    fixture.detectChanges();
    await fixture.whenStable();
  });

  afterEach(() => {
    root.remove();
  });

  it('emits confirmed exactly once when the confirm button is clicked', () => {
    button('Delete').click();

    expect(host.confirmedCount).toBe(1);
    expect(host.cancelledCount).toBe(0);
  });

  it('emits cancelled when Escape is pressed', () => {
    const event = press(dialog(), 'Escape');

    expect(host.cancelledCount).toBe(1);
    expect(host.confirmedCount).toBe(0);
    expect(event.defaultPrevented).toBe(true);
  });

  it('emits cancelled once when the native cancel event follows', () => {
    const cancel = new Event('cancel', { cancelable: true });
    dialog().dispatchEvent(cancel);

    expect(host.cancelledCount).toBe(1);
    expect(cancel.defaultPrevented).toBe(true);
  });

  it('emits cancelled when the cancel button is clicked', () => {
    button('Cancel').click();

    expect(host.cancelledCount).toBe(1);
  });

  it('has role="alertdialog" with labelled title and described message', () => {
    const el = dialog();
    expect(el.getAttribute('role')).toBe('alertdialog');
    expect(el.getAttribute('aria-modal')).toBe('true');

    const title = root.querySelector(`#${el.getAttribute('aria-labelledby')}`);
    const message = root.querySelector(`#${el.getAttribute('aria-describedby')}`);
    expect(title?.textContent?.trim()).toBe('Delete resume?');
    expect(message?.textContent?.trim()).toBe('The file and its extraction will be removed.');
  });

  it('opens on render and moves focus to the cancel button', () => {
    expect(dialog().open).toBe(true);
    expect(document.activeElement).toBe(button('Cancel'));
  });

  it('traps focus: Tab on the last button wraps to the first', () => {
    const confirm = button('Delete');
    confirm.focus();

    const event = press(confirm, 'Tab');

    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(button('Cancel'));
  });

  it('traps focus: Shift+Tab on the first button wraps to the last', () => {
    const cancel = button('Cancel');
    cancel.focus();

    const event = press(cancel, 'Tab', true);

    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(button('Delete'));
  });

  it('restores focus to the previously focused element when removed', async () => {
    host.open.set(false);
    fixture.detectChanges();
    await fixture.whenStable();

    const opener = root.querySelector<HTMLButtonElement>('#opener');
    opener?.focus();
    host.open.set(true);
    fixture.detectChanges();
    await fixture.whenStable();
    expect(document.activeElement).toBe(button('Cancel'));

    host.open.set(false);
    fixture.detectChanges();
    await fixture.whenStable();
    expect(document.activeElement).toBe(opener);
  });
});
