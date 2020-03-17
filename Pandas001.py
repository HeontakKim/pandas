import pandas
import mymodule

# 1강. Dataframe & Series

# dataframe은 pandas의 class입니다.
# dataframe은 default는 csv파일에서 읽어옵니다.
dataframe = pandas.read_csv("temp.csv")
mymodule.printline(dataframe)

# dataframe의 일부만을 출력 가능합니다.
# head는 처음부터, tail은 끝줄부터 셉니다.
mymodule.printline(dataframe.head(2))
mymodule.printline(dataframe.tail(2))
mymodule.printline(dataframe.head())
mymodule.printline(dataframe.tail())

# series는 pandas의 class입니다.
# series는 list로 만듭니다.
s1 = pandas.core.series.Series([1, 2, 3])
s2 = pandas.core.series.Series(['one', 'two', 'three'])
mymodule.printline(s1)
mymodule.printline(s2)

# dataframe은 series의 집합입니다.
# dataframe은 series를 dictionary로 묶어서 만듭니다.
dataframe = pandas.DataFrame(data=dict(num = s1, word = s2))
mymodule.printline(dataframe)

# 각 column을 선택하는 두 가지 방법
print(dataframe.word)
print(dataframe['num'])