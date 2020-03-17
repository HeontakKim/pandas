import pandas as pd
import mymodule

# 8. Group

student_list = [
    ["Kim", "CS", "male"],
    ["Park", "CS", "male"],
    ["Choi", "EE", "male"],
    ["Ryu", "Physics", "male"],
    ["Lee", "Economics", "female"],
    ["Jenny", "CS", "female"],
    ["John", "Physics", "male"]
]

df = pd.DataFrame(student_list, columns=["name", "major", "sex"])
mymodule.printline(df)

# 1) group 이라는 class를 사용할 수 있습니다.
groupby_major = df.groupby("major")

print(groupby_major.groups) # dictionary입니다.
print(type(groupby_major.groups))

print(groupby_major.name)
print(groupby_major.sex)

for name, group in groupby_major: # 시각적으로 좋은 print를 위해
    print(name + " : " + str(len(group)))
    print(group)
    print()

# 2) 학과별 인원을 dataframe으로 만들고 싶어요.
df_major_cnt = pd.DataFrame( {'count' : groupby_major.size()} )
mymodule.printline(df_major_cnt)

# 3) 성별으로 group 만들기
groupby_sex = df.groupby("sex")
for name, group in groupby_sex: # 시각적으로 좋은 print를 위해
    print(name + " : " + str(len(group)))
    print(group)
    print()
