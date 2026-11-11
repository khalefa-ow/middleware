import json, time, litellm, llm_wrapper, sys
h = llm_wrapper.register(sys.argv[1])
sent = []
w = h.wrapper
real = w.acompletion
async def spy(**kw):
    sent.append(kw["messages"]); return await real(**kw)
w.acompletion = spy
msgs = [{"role": "system", "content": "You are a terse assistant."},
        {"role": "user", "content": "Write a one-line reminder to email jane.doe@example.com and call +1 415 555 0100 about invoice 7. Include the email and phone exactly."}]
t = time.time()
r = litellm.completion(model="wrapper/auto", messages=msgs, temperature=0)
print("latency", round(time.time()-t, 2), "s")
print("SENT USER:", sent[0][-1]["content"])
print("MODEL SAW HINT:", "Keep every placeholder" in sent[0][0]["content"])
print("ANSWER:", r.choices[0].message.content)
print("usage", r.usage.prompt_tokens, r.usage.completion_tokens, "wrapper", r._hidden_params.get("wrapper"))
print("leaked?", any(x in json.dumps(sent) for x in ["jane.doe@example.com", "555 0100"]))
s = litellm.completion(model="wrapper/auto", messages=msgs, temperature=0, stream=True)
print("STREAM:", "".join(c.choices[0].delta.content or "" for c in s))
w.store.flush()
