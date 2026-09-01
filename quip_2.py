"""Salutation lines for automated student reminders.

QUIPS is context-free: it pairs with any reminder body (deadline, exam, study,
doubt, fee, form, anything). The themed tuples below are opinionated and assume
their context. ALL_QUIPS maps a reminder kind to the pool that fits it.

Tone mix throughout: ~40% humble+sharp, ~20% sassy, ~20% AI-vs-humans,
~20% sarcastic. Hinglish stays around 15% so it reads as spice, not default.
Nothing here is meant to sting the quiet people who are furthest behind.
"""

from __future__ import annotations

import random

__all__ = [
    "QUIPS",
    "DEADLINE_QUIPS",
    "EXAM_QUIPS",
    "STUDY_QUIPS",
    "DOUBT_QUIPS",
    "ALL_QUIPS",
    "pick",
]


# --------------------------------------------------------------------------
# Generic — safe for every reminder type. No verb assumptions, no "submit".
# --------------------------------------------------------------------------

QUIPS: tuple[str, ...] = (
    # humble & sharp
    "Small thing, still pending. Let's fix that.",
    "This one's been waiting patiently. Its patience is finite.",
    "Quick one — costs less time than remembering it does.",
    "Ten minutes now, zero panic later.",
    "A nudge. Not a judgement, just a nudge.",
    "You already know this one. It just needs doing.",
    "Chhota kaam hai. Isiliye har baar reh jaata hai.",
    "The thinking is done. Only the doing is left.",
    "Putting this on today, not on someday.",
    "One item. Then peace.",
    "Start badly. Momentum handles the rest.",
    "This is easier now than it will be later. It always is.",
    "Not urgent yet — which is exactly why now is a good time.",
    "Aaj kar lo, kal free.",
    "Low effort, high relief. Rare combination.",
    "The reminder was the easy part. Over to you.",
    "Reading this is not the same as doing this.",
    "Future you is quietly hoping.",
    "It's on the list. Lists don't clear themselves.",
    "No drama needed. Just this one thing.",
    "Twenty focused minutes is the whole ask.",
    "Half-done today beats perfect never.",
    "You've handled harder things quietly this semester. This too.",
    "Bas ek kaam. Phir chill.",
    # sassy
    "Second time I'm mentioning this. There will be a third.",
    "Still open. Still waiting. Still yours.",
    "I have excellent memory and absolutely no shame.",
    "Marking this read is not the same as acting on it.",
    "Someone here has already done it. They are insufferably calm.",
    "Swipe it away if you like. I'll be back.",
    "Haan, yeh reminder tumhare liye hi hai.",
    "Consider this the friendly version.",
    "The group is quiet. The task is not.",
    "Dekh liya? Ab kar bhi lo.",
    "Being ignored beautifully, as always.",
    "Everyone assumes someone else is further behind. Statistically, no.",
    # AI vs humans
    "The machines are learning. This is still pending.",
    "Everything else in your stack runs on schedule. This doesn't.",
    "You automate pipelines for a living. Try automating this.",
    "AI can do a great deal. It cannot do this on your behalf.",
    "A cron job somewhere is doing its part right now. Be inspired.",
    "No model required here. Just a human, briefly.",
    "Your attention mechanism has drifted. Recalibrate.",
    "Gradient descent works by taking one step. Take it.",
    "GPT se plan bana lo, karna toh khud hi padega.",
    "Latency between knowing and doing is unusually high today.",
    "Training runs finish. Human loops stall. Curious.",
    "Even a random baseline does something.",
    # sarcastic & funny
    "Time is a construct. Calendars are not.",
    "Bold approach so far. Results pending.",
    "This has been 'almost done' for a suspiciously long while.",
    "Panic is scheduled for later. Beat the rush.",
    "The universe is indifferent. The calendar is not.",
    "Do it now and earn the right to relax convincingly.",
    "The Wi-Fi will fail at the worst possible moment. Plan accordingly.",
    "Hope is not a strategy, though it remains very popular.",
    "This message will keep returning until something happens.",
    "Legend speaks of someone doing it early. Unverified.",
    "Karna toh hai hi. Timing tumhari choice hai.",
    "That's the reminder. That's the entire content.",
)


# --------------------------------------------------------------------------
# Deadlines — assignments, quizzes, submissions.
# --------------------------------------------------------------------------

DEADLINE_QUIPS: tuple[str, ...] = (
    # humble & sharp
    "Last day. Nobody's coming to submit it for you. That's the whole design.",
    "Deadline's today. Small task, short window, no mystery.",
    "Today's the day. Kaam chhota hai, delay bada.",
    "Last day. You already know what to do. That's the annoying part.",
    "Deadline today. Ten minutes now beats two hours of dread.",
    "Last day. The hardest part is opening the file. Everything after is downhill.",
    "Deadline's today. This doesn't need brilliance. It needs a click.",
    "Today. Done beats perfect, and both beat nothing.",
    "Last day. Perfection is a good excuse and a bad plan.",
    "Deadline today. The version you have beats the one you're imagining.",
    "Last day. You have time. You do not have infinite time.",
    "Deadline's today. Future you would like a word. Politely.",
    "Today's the day. It's one submission, not a thesis defence.",
    "Last day. Start badly, fix later, submit.",
    "Deadline today. Your draft is closer than your anxiety says it is.",
    "Last day. Half-done and submitted counts. Half-done and saved does not.",
    "Deadline's today. The list gets shorter exactly one way.",
    "Today. Waiting for motivation is a scheduling error.",
    "Last day. Aaj kar lo, raat ko chain se so jaoge.",
    "Deadline today. Nobody's grading the struggle. Only the submission.",
    "Last day. The gap between 'almost done' and 'done' is smaller than it feels.",
    "Deadline's today. Twenty focused minutes. That's the entire ask.",
    "Today's the day. It'll take less time than the guilt already has.",
    "Last day. Chai first if you must. Submit after.",
    "Deadline today. Bas kar do, aaj hi.",
    "Last day. Aaj nahi toh kab — genuinely asking.",
    "Deadline's today. The clock has no opinion about your reasons.",
    "Today. One task, one tab, go.",
    "Last day. You've done harder things this semester quietly. Do this one too.",
    "Deadline today. Momentum is cheaper to start than to restart.",
    "Last day. Submit the imperfect one. It still counts as submitted.",
    "Deadline's today. The only bad version is the unsubmitted one.",
    "Today's the day. Close the twelve tabs. Keep one.",
    "Last day. Your calendar warned you. Twice. Nicely.",
    "Deadline today. This is the easy part of the degree. Take the free marks.",
    "Last day. Nothing improves by staying in drafts.",
    "Deadline's today. Do it now while it's still a task and not a problem.",
    "Today. Chhota kaam hai — isiliye har baar reh jaata hai.",
    "Last day. Finish it and go back to your actual life.",
    "Deadline today. No drama required. Just the file.",
    # sassy
    "Last day. Yes, this is the third message. Yes, there's a reason.",
    "Deadline's today. I stop when you submit. That's the whole arrangement.",
    "Today's the day. This reminder has better attendance than some of you.",
    "Last day. Reading this counts as neither progress nor submission.",
    "Deadline today. Marking this read does not mark the task done.",
    "Last day. Someone here finished on day one. They are very calm right now.",
    "Deadline's today. The group is quiet. The deadline is not.",
    "Today. Padhai baad mein, submission pehle.",
    "Last day. 'Kal karta hoon' ka kal aa gaya.",
    "Deadline today. Extension maangne se pehle ek baar try toh kar lo.",
    "Last day. Swiping this away does not submit it. Tested. Confirmed.",
    "Deadline's today. Impressive commitment to the final hour, as always.",
    "Today's the day. Everyone saw the last reminder. Very few acted on it.",
    "Last day. Yes, today. Not 'today-ish'.",
    "Deadline today. A screenshot in the group is not a submission receipt.",
    "Last day. Bunk kar sakte ho, submit toh kar do.",
    "Deadline's today. Scrolling past this costs roughly what starting would.",
    "Today. Consider this the polite version.",
    "Last day. Everyone assumes someone else is more behind. Statistically, no.",
    "Deadline today. Group mein 'done kiya?' poochne se pehle khud kar lo.",
    # AI vs humans
    "Last day. The model converged, the pipeline ran. You're the bottleneck.",
    "Deadline's today. Your loss function is not going down on its own.",
    "Today's the day. AI can draft your abstract. It cannot submit your regret.",
    "Last day. Somewhere a cron job is finishing on time. Be inspired.",
    "Deadline today. MTech in AI, and this is still being done manually. Poetic.",
    "Last day. The machines are learning. You are pending.",
    "Deadline's today. No epochs left, no early stopping.",
    "Today. You've read four papers on optimization. Try applying one.",
    "Last day. AI will not take your job. This deadline might take your CGPA.",
    "Deadline today. Even your overnight fine-tune converged faster than this.",
    "Last day. Automate everything else. This one still needs a human in the loop.",
    "Deadline's today. Your attention mechanism is clearly focused elsewhere.",
    "Today's the day. Batch size one, deadline one. Do the math.",
    "Last day. Gradient descent works because it takes a step. Any step.",
    "Deadline today. LLM se likhwa lo, submit toh khud hi karna padega.",
    "Last day. Overfitting to Instagram, underfitting to coursework.",
    "Deadline's today. The training loop finished. The human loop is stuck.",
    "Today. Prompt engineering will not help if you never hit submit.",
    "Last day. Your model generalises better than your excuses.",
    "Deadline today. Even a random baseline submits something.",
    # sarcastic & funny
    "Last day. Bold strategy so far. Let's see how it plays out.",
    "Deadline's today. Panic peaks around 9 pm. Beat the rush.",
    "Today's the day. The deadline has been unusually patient with you.",
    "Last day. Good news: after today you stop thinking about it. Either way.",
    "Deadline today. The calendar and I discussed this. It agrees with me.",
    "Last day. Time is a construct. The submission portal is not.",
    "Deadline's today. Yes, the portal actually closes. It's rude like that.",
    "Today. Nothing says confidence like starting at 11:47 pm.",
    "Last day. Plan B is hope. Plan A is still available.",
    "Deadline today. The server does not accept vibes.",
    "Last day. 'Almost done' has been the status for six days. Remarkable stability.",
    "Deadline's today. Legend says someone once submitted early. Unverified.",
    "Today's the day. Your excuses are beautifully structured. Submit those instead?",
    "Last day. Extension ki afwaah abhi tak confirm nahi hui hai.",
    "Deadline today. The Wi-Fi will fail exactly when you need it. Plan accordingly.",
    "Last day. Last-minute submissions build character. You have enough character.",
    "Deadline's today. Somewhere a professor is refreshing the list. Make their day.",
    "Today. Your future self is watching. Mildly concerned.",
    "Last day. Submit now and spend tonight pretending you always had it handled.",
    "Deadline today. That's it. That's the message.",
)


# --------------------------------------------------------------------------
# Exam announcements — "the exam is on <date>".
# --------------------------------------------------------------------------

EXAM_QUIPS: tuple[str, ...] = (
    "Exam date is fixed. Your schedule is the flexible part.",
    "Marking your calendar so you can't claim you didn't know.",
    "This date will not move. Everything else in your week can.",
    "Consider this the early warning, not the emergency.",
    "Plenty of time — which is how people end up with none.",
    "Date ready. Syllabus ready. Only you are optional so far.",
    "Exam aa raha hai. Chup-chaap, par aa raha hai.",
    "Countdown started. It runs whether you look at it or not.",
    "Same syllabus for everyone. Different start dates, though.",
    "Filing this under 'known in advance', so no surprises later.",
    "The exam has been scheduled with great confidence in you.",
    "No deadline extension exists for this one. Different genre entirely.",
    "You cannot fine-tune the night before. Well — you can. It shows.",
    "Test set arrives on this date. No peeking, no leakage.",
    "The syllabus is not going to shrink between now and then.",
    "Date noted. Ab planning bhi kar lo.",
    "Enough runway to be calm. Not enough to be careless.",
    "A model trained the night before generalises badly. So do you.",
    "Save the date. Genuinely, save it.",
    "This is the boring reminder that prevents the dramatic week.",
)


# --------------------------------------------------------------------------
# Study nudges — revision, prep, reading.
# --------------------------------------------------------------------------

STUDY_QUIPS: tuple[str, ...] = (
    "One hour today is worth four the night before. Bad exchange rate later.",
    "Revision now is cheap. Revision later is expensive and loud.",
    "Open the notes. That's the whole task for the next five minutes.",
    "You don't need a perfect study plan. You need a started one.",
    "Small daily epochs beat one heroic overnight run.",
    "Aaj thoda padh lo, exam week mein khud ko thank karoge.",
    "Distributed practice beats cramming. There's literature. You've read it.",
    "The syllabus does not get smaller by being avoided.",
    "Twenty minutes. Timer on. Phone somewhere else.",
    "Understanding one topic well beats skimming six badly.",
    "Your notes are useless in a folder. Same as an untrained model.",
    "Start with the topic you've been avoiding. You know which one.",
    "Padhai ka mood nahi aata — banana padta hai.",
    "Rest counts too. But rest after something, not instead of it.",
    "You revise for the exam, not for the group chat's benefit.",
    "The machines study 24x7 and still need epochs. Give yourself one.",
    "No shortcuts today, just a start.",
    "Reading a paper counts. Bookmarking it does not.",
    "Consistency is boring and it works. Inconvenient combination.",
    "Half an hour now, or a panicked all-nighter later. Your call.",
)


# --------------------------------------------------------------------------
# Doubts / asking a TA or instructor.
# --------------------------------------------------------------------------

DOUBT_QUIPS: tuple[str, ...] = (
    "Ask the doubt. Nobody has ever been marked down for asking.",
    "That question you've been carrying for a week — ask it today.",
    "The TA is paid to answer. Let them earn it.",
    "If you're confused, four other people are too, and quieter.",
    "Doubt poochne mein kuch nahi jaata. Na poochne mein marks jaate hain.",
    "Twenty minutes of guessing, or two minutes of asking. Optimise.",
    "The TA cannot read your mind. Nobody's model is that good yet.",
    "Asking early is cheap. Asking the night before is not.",
    "Nobody thinks less of a question. Everyone remembers a blank answer.",
    "Ask now while it's a doubt, not later when it's a gap.",
    "You debugged the code for three hours. Try a human for three minutes.",
    "The stupid question is the one asked after the exam.",
    "Sharam chhodo, doubt poocho.",
    "The TA's inbox is more forgiving than the answer sheet.",
    "One message. That's the entire task.",
)


ALL_QUIPS: dict[str, tuple[str, ...]] = {
    "generic": QUIPS,
    "deadline": DEADLINE_QUIPS,
    "exam": EXAM_QUIPS,
    "study": STUDY_QUIPS,
    "doubt": DOUBT_QUIPS,
}


def pick(
    kind: str = "generic",
    *,
    exclude: frozenset[str] | set[str] | None = None,
    rng: random.Random | None = None,
) -> str:
    """Return one line for the given reminder kind.

    Themed pools fall back to the generic pool when every themed line is in
    ``exclude`` (useful for avoiding recent repeats in a rotation queue).

    Raises:
        KeyError: if ``kind`` is not a known reminder kind.
    """
    pool = ALL_QUIPS[kind]
    chooser = rng or random
    if exclude:
        candidates = tuple(q for q in pool if q not in exclude)
        if not candidates and kind != "generic":
            candidates = tuple(q for q in QUIPS if q not in exclude)
        pool = candidates or pool
    return chooser.choice(pool)


if __name__ == "__main__":
    for name, pool in ALL_QUIPS.items():
        print(f"{name:9s} {len(pool):3d} lines | {pick(name)}")