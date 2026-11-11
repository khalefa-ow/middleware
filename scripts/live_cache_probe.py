import litellm, llm_wrapper, sys, json
h = llm_wrapper.register(sys.argv[1])
w = h.wrapper
sent = []
real = w.acompletion
async def spy(**kw):
    sent.append(json.dumps(kw["messages"])); return await real(**kw)
w.acompletion = spy
system = "You are a code reviewer for team lead jane.doe@example.com. " + " ".join(f"Rule {i}: keep functions short and named clearly." for i in range(300))
conv = [{"role": "system", "content": system}, {"role": "user", "content": "Reply with one word: ready?"}]
for turn in range(3):
    r = litellm.completion(model="wrapper/auto", messages=conv, temperature=0, max_tokens=5)
    u = r.usage
    det = getattr(u, "prompt_tokens_details", None)
    print(f"turn {turn}: prompt {u.prompt_tokens} cached {getattr(det,'cached_tokens',None)} "
          f"hit {getattr(u,'prompt_cache_hit_tokens',None)} wrapper_cost {r._hidden_params['wrapper']['cost_usd']}")
    conv += [{"role": "assistant", "content": r.choices[0].message.content}, {"role": "user", "content": f"Again {turn}?"}]
pre = [s[:len(sent[0]) - 200] for s in sent]
print("outbound prefix identical across turns:", len(set(pre)) == 1, "| email sent:", "jane.doe" in "".join(sent))
try:
    print("completion_cost:", litellm.completion_cost(completion_response=r))
except Exception as e:
    print("completion_cost error", e)
