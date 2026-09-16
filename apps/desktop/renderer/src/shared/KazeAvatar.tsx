/** Shared app mark; adjacent text supplies the accessible name. */
export function KazeAvatar({ size = 28 }: { size?: number }) {
  return <img className="kaze-avatar" src="./assets/branding/kaze-avatar.png" width={size} height={size} alt="" aria-hidden="true" draggable={false} />;
}
