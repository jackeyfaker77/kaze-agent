import type { ModelRegistrationFormData } from "../../../src/bridge/shared";
/** Removes a connection from the application-wide model catalog. */
export async function prepareModelRegistrationRemoval(registration: ModelRegistrationFormData, registrations: ModelRegistrationFormData[]): Promise<boolean> {
  if (registrations.length <= 1) return false;
  return window.confirm(`删除模型 ${registration.model}？`);
}
