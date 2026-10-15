// Detalle de cotización: modal para agregar artículos y confirmación de la reserva.
document.addEventListener('DOMContentLoaded', () => {
  const modal = document.getElementById('modal-agregar-articulo');
  document.getElementById('btn-agregar-articulo')?.addEventListener('click', () => {
    modal.hidden = false;
    modal.querySelector('select')?.focus();
  });

  // El estado solo cambia en pantalla después de que el servidor confirma; aquí solo se pide confirmación.
  const formConfirmar = document.getElementById('form-confirmar');
  formConfirmar?.addEventListener('submit', async (e) => {
    if (formConfirmar.dataset.confirmado) return;
    e.preventDefault();
    const opciones = {
      titulo: 'Confirmar reserva',
      mensaje: 'Se volverá a comprobar el stock para las fechas del evento. Si alcanza, la reserva quedará confirmada y retendrá esos artículos.',
      textoConfirmar: 'Confirmar reserva',
    };
    const ok = typeof confirmarAccion === 'function'
      ? await confirmarAccion(opciones)
      : window.confirm(opciones.mensaje);
    if (!ok) return;
    formConfirmar.dataset.confirmado = '1';
    const boton = formConfirmar.querySelector('button[type="submit"]');
    boton.disabled = true;
    boton.textContent = 'Confirmando...';
    formConfirmar.submit();
  });
});
