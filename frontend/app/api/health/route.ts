/**
 * Frontend liveness. Deliberately does NOT check the control plane.
 *
 * If it did, a backend blip would fail the frontend's liveness probe and
 * Kubernetes would restart the dashboard pods -- turning one outage into two,
 * and taking away the UI exactly when someone needs it to see what is wrong.
 */
export const dynamic = "force-dynamic";

export function GET() {
  return Response.json({ status: "ok", component: "frontend" });
}
