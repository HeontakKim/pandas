import pandas as pd
import mymodule

# 6. 행, 열 삭제하기

friends = [
    {"age":20, "job":"Trash"},
    {"age":30, "job":"Animal"},
    {"age":40, "job":"Streamer"},
    {"age":50}
]
df = pd.DataFrame(friends,
    index=["Kim", "Lee", "Park", "Ryu"]
)
print("DataFrame:")
mymodule.printline(df)

# 1) 삭제하기
# index 이름이 dataframe index 중 없으면 error 발생
df2 = df.drop(["Park", "Ryu"])
print("1) 삭제하기")
mymodule.printline(df2)

# 2) 이렇게 하면 바로 df에 적용된다.
# 즉, df = df.drop(...) 형태를 대신한다.
df.drop(["Lee"], inplace=True)
print("2) 이렇게 하면 바로 df에 적용된다.")
mymodule.printline(df)

# 새로운 dataframe (index를 숫자로 한다)
friends = [
    {"name":"Kim", "age":20, "job":"Trash"},
    {"name":"Lee", "age":30, "job":"Animal"},
    {"name":"Park", "age":40, "job":"Streamer"},
    {"name":"Ryu", "age":50}
]
df = pd.DataFrame.from_dict(friends)
print("New DataFrame:")
mymodule.printline(df)

# 3) 숫자로 index로 row 삭제하기
df2 = df.drop([0,2])
print("3-1) 숫자로 index로 row 삭제하기")
mymodule.printline(df2)
df2 = df.drop(df.index[[1,3]])
print("3-2) 숫자로 index로 row 삭제하기")
mymodule.printline(df2)

# 4) column의 조건을 이용해 row drop하기
df2 = df[df.age > 30]
print("4) column의 조건을 이용해 row drop하기")
mymodule.printline(df2)

# 5) column 제거하기 (drop 메소드에서 axis를 1로 하면 됨)
df2 = df.drop("age", axis=1)
print("5) column 제거하기")
mymodule.printline(df2)