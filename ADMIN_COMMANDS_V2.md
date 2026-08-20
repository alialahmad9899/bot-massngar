# Admin Command Center V2

The existing admin mode remains backward compatible. This extension adds structured commands that are validated before touching SQLite.

## Courses

- `أضف دورة ميك أب متقدم، 16 درس، السعر 900000، الدفعة الأولى 250000، تبدأ 2026-09-01`
- `غيّر سعر دورة ميك أب متقدم إلى 950000`
- `غيّر سعر دورة ميك أب متقدم إلى 950000 وغيّر الدفعة الأولى إلى 250000`
- `تراجع عن آخر تعديل لدورة ميك أب متقدم`
- `احذف دورة ميك أب متقدم` ثم `نعم` للتأكيد (الحذف المنطقي يعطّل الدورة)

## Schedules

- `أضف موعد بدء لدورة ميك أب متقدم: 2026-09-15`
- `أضف موعد بدء لدورة ميك أب متقدم: 2026-09-15، دوام: الاثنين والأربعاء والجمعة`

## Information

- `أضف معلومة سياسة الاسترجاع = لا يوجد استرجاع`
- `عدّل معلومة سياسة الاسترجاع = وفق شروط الإدارة`
- `احذف معلومة سياسة الاسترجاع` ثم `نعم`

## Offers

- `أضف عرض مكياج: خصم 100000، من 2026-09-01 إلى 2026-09-10`

Only offers whose date window contains today's date are injected into the dynamic academy knowledge.

## CRM / Analytics

- `اعرض العملاء الساخنين`
- `مين العملاء المهتمين`
- `احصائيات البوت`

Lead scoring is updated for normal customer messages. Higher intent signals such as asking about price, payment, registration, or Sham Cash raise the score.

## Safety

Destructive commands use the existing admin confirmation channel. Every structured write records before/after snapshots in `academy_change_history` so a course update can be rolled back.
