---
subject: A question about how {{ account.name }} builds
---
Hello {{ contact.first_name }},

My name is {{ persona.first_name }}, and I {{ persona.role }} at {{ persona.org }}.
{% if personalization %}
{{ personalization }}
{% endif %}
I work with teams applying models to a specific domain problem rather than training their own, and the hard part is usually evaluation rather than modelling — knowing whether a change actually helped. If that is a live question at {{ account.name }}, I would be glad to compare notes.

{{ persona.signature }}
