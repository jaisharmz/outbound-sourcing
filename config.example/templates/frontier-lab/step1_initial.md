---
subject: Research collaboration between {{ persona.org }} and {{ account.name }}
---
Hello {{ contact.first_name }},

I hope this finds you well. My name is {{ persona.first_name }}, and I {{ persona.role }} at {{ persona.org }}. A short summary of our recent work:

{{ persona.project_bullets }}
{% if personalization %}
{{ personalization }}
{% endif %}
I know an external collaboration at an organization your size usually goes through a formal channel rather than an individual. If there is a partnerships or university-relations process I should be talking to, I would be glad to be pointed at it — and if there is something smaller that fits inside work already underway, I would rather start there.

{{ persona.signature }}
