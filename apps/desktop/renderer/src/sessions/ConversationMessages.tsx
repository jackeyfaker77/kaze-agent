import { memo } from "react";
import type { Message } from "./api";
import { KazeAvatar } from "../shared/KazeAvatar";
import { MessageParts } from "./MessageParts";
import { interruptedMessage, partsFromMessage, thinkingDuration, type TurnDisplay } from "./conversationDisplay";
import { Markdown } from "./primitives";

export const ConversationMessage = memo(function ConversationMessage({ message, turn }: { message: Message; turn?: TurnDisplay }) {
  return <article className={`chat-message ${message.role}`}>
    <small>{message.role === "user" ? "你" : <><KazeAvatar size={28} /><span>Kaze</span></>}</small>
    {message.role === "assistant" ? <MessageParts parts={turn?.parts ?? partsFromMessage(message)}
      running={turn?.phase === "running"} awaitingHistory={turn?.phase === "awaiting-history"}
      interrupted={interruptedMessage(message)} durationMs={thinkingDuration(message)} /> : <Markdown>{message.content}</Markdown>}
    {message.media?.map((path, index) => <p className="attachment-name" key={index}>附件：{path.split(/[\\/]/).at(-1)}</p>)}
  </article>;
});
